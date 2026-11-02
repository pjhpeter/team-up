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

# 2. Herdr 插件的角色配置：指向仓库里的 herdr/config.yaml
dir=$(herdr plugin config-dir herdr-workflows 2>/dev/null || echo "$HOME/.config/herdr/plugins/config/herdr-workflows")
mkdir -p "$dir"
target="$dir/config.yaml"
src="$HWF/herdr/config.yaml"
if grep -q "/Users/pengjiahui/.hwf" "$src" && [ "$HOME" != "/Users/pengjiahui" ]; then
  # 别人的机器：路径不同，复制一份并替换路径（以后改了仓库要重跑本脚本）
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

# 3. 依赖检查
python3 -c 'import yaml' 2>/dev/null && echo "[已有]   python3 + PyYAML（只有测试用到）" || echo "[注意]   测试需要 PyYAML：python3 -m pip install pyyaml"
for c in herdr glab omp claude codex; do
  command -v "$c" >/dev/null 2>&1 && echo "[已有]   $c" || echo "[注意]   没装 $c（角色配置里用到）"
done
echo "装好了。每个要接入的仓库跑一次：python3 $HWF/scripts/init-project.py <仓库路径> --apply"
