# team-up：Herdr 需求交付流程

**这是什么**：一套让多个 AI Agent 按「方案 → 开发 → 评审 → 测试 → 上线」接力交付 GitLab 需求的流程。规则、脚本、角色配置都在这里，本机的 `~/.hwf` 就是本仓库的副本。

## 装到一台电脑上

```sh
git clone git@git.bootingai.net:ai-hub/skills/workflow/team-up.git ~/.hwf
sh ~/.hwf/install.sh
```

然后让每个要用流程的仓库接入一次（先不加 `--apply` 看一遍要做什么）：

```sh
python3 ~/.hwf/scripts/init-project.py ~/code/<仓库> --apply --smoke "国内=https://<测试环境地址>"
```

## 里面有什么

| 位置 | 作用 |
| --- | --- |
| `PROTOCOL.md` | 流程规则本体，所有角色都按它干活 |
| `workflows/team-up.yaml` | Herdr 启动团队的入口 |
| `herdr/config.yaml` | 各角色用什么工具、什么模型（install.sh 把 Herdr 插件配置链接到这里） |
| `scheduler-omp.yml` | 调度者的上下文压缩设置 |
| `scripts/` | 收件箱、找调度者、成员收尾、清理临时目录、新项目接入等脚本 |
| `templates/project/` | 新项目接入时写进仓库的文件模板 |
| `tests/` | 脚本的单元测试，CI 每次推送都跑 |

不入库（每台电脑自己的）：`state/`（运行状态）、`gitlab-inbox.config.json`（本机仓库路径，模板见 `gitlab-inbox.config.example.json`）。

## 怎么改

1. 在 `~/.hwf` 里建分支：`git switch -c <改什么>`。
2. 改完跑测试：`python3 -m unittest discover -s tests -p 'test_*.py'`。
3. 推送并开 MR，合并后其他电脑 `git -C ~/.hwf pull` 即生效（改了 `herdr/config.yaml` 且不是本人电脑的，再跑一次 `install.sh`）。

改动立即对本机生效：正在跑的调度者下一次读协议时就会用新规则。
