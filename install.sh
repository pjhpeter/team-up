#!/bin/sh
# 把本仓库装成本机的 Herdr 交付流程。可重复运行。
#   git clone git@git.bootingai.net:ai-hub/skills/workflow/team-up.git ~/.hwf
#   sh ~/.hwf/install.sh
set -eu
HWF="$HOME/.hwf"
here=$(cd "$(dirname "$0")" && pwd)
[ "$here" = "$HWF" ] || { echo "本仓库必须克隆在 $HWF（协议和脚本按这个路径找文件），现在在 $here"; exit 1; }

mkdir -p "$HWF/state"

# 1. 收件箱：共用配置 gitlab-inbox.config.json 已入库；本机只需 gitlab-inbox.local.json 写仓库路径
if [ ! -f "$HWF/gitlab-inbox.local.json" ]; then
  printf '{\n  "projects": {}\n}\n' > "$HWF/gitlab-inbox.local.json"
  echo "[已新建] gitlab-inbox.local.json：对每个仓库跑 init-project.py --apply 会自动填上本机路径"
else
  echo "[已有]   gitlab-inbox.local.json"
fi

# 2. 角色本机覆盖模板与 Herdr 插件配置：roles.local.yaml 不入库，只写你想改的角色
if [ ! -f "$HWF/roles.local.yaml" ]; then
  cat <<'EOF' > "$HWF/roles.local.yaml"
# 本机个人角色覆盖配置（不入库，不会随 git 推送到远端）。
# 只需写你想覆盖的角色，未写的角色自动继承 ~/.hwf/roles.yaml：
#   - 换 tool（例如 codex -> droid）时整条角色替换，避免旧工具专属参数串入；
#   - 没换 tool 或省略 tool 时仅覆盖你写的字段（例如单改 thinking 或 model）。
# 10 个交付成员与 4 个后台服务改完免重启自动生效；改动 scheduler 后运行一次：
#   python3 ~/.hwf/scripts/role_config.py sync
# 查看当前生效配置与来源：
#   python3 ~/.hwf/scripts/role_config.py show
#
# team:
#   arch:
#     tool: droid
#     model: gemini-4-carbon
#     thinking: high
#   dev:
#     thinking: high
#
# services:
#   secretary:
#     thinking: low
EOF
  echo "[已新建] roles.local.yaml（本机个人覆盖模板，不入库）"
else
  echo "[已有]   roles.local.yaml"
fi

if [ -f "$HWF/roles.yaml" ] && python3 -c 'import yaml' 2>/dev/null; then
  python3 "$HWF/scripts/role_config.py" sync
fi
dir=$(herdr plugin config-dir herdr-workflows 2>/dev/null || echo "$HOME/.config/herdr/plugins/config/herdr-workflows")
mkdir -p "$dir"
target="$dir/config.yaml"
if [ -f "$HWF/herdr/config.local.yaml" ]; then
  src="$HWF/herdr/config.local.yaml"
else
  src="$HWF/herdr/config.yaml"
fi
if grep -q "/Users/pengjiahui/.hwf" "$src" && [ "$HOME" != "/Users/pengjiahui" ]; then
  # 别人的机器且未装 PyYAML 无法 sync：复制一份并替换路径
  [ -e "$target" ] && [ ! -L "$target" ] && cp -p "$target" "$target.bak-$(date +%Y%m%d%H%M%S)"
  rm -f "$target"
  sed "s#/Users/pengjiahui/.hwf#$HWF#g" "$src" > "$target"
  echo "[已复制] Herdr 角色配置（已替换成本机路径）→ $target"
elif [ -L "$target" ] && [ "$(readlink "$target")" = "$src" ]; then
  echo "[已有]   Herdr 角色配置链接"
else
  [ -e "$target" ] && cp -p "$target" "$target.bak-$(date +%Y%m%d%H%M%S)"
  ln -sfn "$src" "$target"
  echo "[已链接] Herdr 角色配置 → $src"
fi

# 3. omp 扩展 + agy Hooks：调度者改 INDEX、发 issue 评论、停回合忘挂等待或弹框/私开子代理时硬拦截
ext="$HOME/.omp/agent/extensions"
if [ -d "$HOME/.omp/agent" ]; then
  mkdir -p "$ext"
  for f in "$HWF"/omp-extensions/*.ts; do
    ln -sfn "$f" "$ext/$(basename "$f")"
  done
  echo "[已链接] omp 扩展 → $HWF/omp-extensions（新开的 omp 会话生效）"
else
  echo "[注意]   没找到 ~/.omp/agent，omp 扩展未安装（调度者用 omp 时需要）"
fi

if [ -d "$HOME/.gemini" ] || command -v agy >/dev/null 2>&1; then
  python3 "$HWF/scripts/agy-scheduler-guard.py" install-hooks
  echo "[已安装] agy 调度者 Hooks → ~/.gemini/config/hooks.json（仅对 delivery-scheduler-* 生效）"
else
  echo "[注意]   未检测到 agy / ~/.gemini，跳过 agy Hooks 安装"
fi

# 4. 成员 skill：链接到所有 Agent 共用的 ~/.agents/skills；Claude Code 只读 ~/.claude/skills，再按其惯例链一层
mkdir -p "$HOME/.agents/skills"
for d in "$HWF"/skills/*/; do
  name=$(basename "$d")
  link="$HOME/.agents/skills/$name"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "[注意]   $link 是已有的普通目录，没有覆盖；确认可删后重跑本脚本"
    continue
  fi
  ln -sfn "$HWF/skills/$name" "$link"
  if [ -d "$HOME/.claude/skills" ] && { [ ! -e "$HOME/.claude/skills/$name" ] || [ -L "$HOME/.claude/skills/$name" ]; }; then
    ln -sfn "../../.agents/skills/$name" "$HOME/.claude/skills/$name"
  fi
  echo "[已链接] skill ${name} → $HWF/skills/$name"
done

# 5. 依赖检查
python3 -c 'import yaml' 2>/dev/null && echo "[已有]   python3 + PyYAML（只有测试用到）" || echo "[注意]   测试需要 PyYAML：python3 -m pip install pyyaml"
for c in herdr glab swiftc omp claude codex droid agy; do
  command -v "$c" >/dev/null 2>&1 && echo "[已有]   $c" || echo "[注意]   没装 $c（角色配置里用到）"
done
echo "装好了。每个要接入的仓库跑一次：python3 $HWF/scripts/init-project.py <仓库路径> --apply"
