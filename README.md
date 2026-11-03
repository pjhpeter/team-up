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
| `scripts/` | 收件箱、飞书桥、找调度者、成员收尾、清理临时目录、新项目接入等脚本 |
| `templates/project/` | 新项目接入时写进仓库的文件模板 |
| `tests/` | 脚本的单元测试，CI 每次推送都跑 |
| `gitlab-inbox.config.json` | 收件箱共用配置：盯哪些 GitLab 项目、只认谁的操作、开关标签、轮询间隔 |

不入库（每台电脑自己的）：`state/`（运行状态）、`gitlab-inbox.local.json`（每个项目在本机的仓库路径，也可覆盖共用配置里的其他值，比如换成你自己的白名单账号）。

收件箱要常驻在一个 Herdr 窗格里：`python3 ~/.hwf/scripts/gitlab-inbox.py`；改了配置或脚本后，在那个窗格按 Ctrl+C 再重新运行。

## 从飞书提需求（可选）

接上之后，在飞书里私聊自己的机器人（或在群里 @它），就能建 issue、和 Agent 对话；Agent 在 issue 里的评论会回到同一个飞书话题。Agent 团队仍只看 GitLab，飞书桥只负责来回搬消息。

| 在飞书里发 | 结果 |
| --- | --- |
| `需求：<内容>` 或 `/需求 <内容>` | 建 issue（打 `herdr-auto`、指派给 Agent），回帖 issue 链接 |
| `#123 <内容>` 或贴 issue 链接 | 把这个话题关联到已有 issue，内容写成评论 |
| 在上面的话题里回复 | 写成该 issue 的评论；`/go`、`/status` 等指令照常生效 |
| 调度者贴出编号选项后回一个数字（如 `1`） | 收件箱核对界面没变，替你在电脑上选中这一项并回帖结果 |

群里使用时，每句话（包括话题里的回复）都要 @机器人，它才收得到；私聊不用。

每人接一套：用 lark-cli 建好机器人应用、存一个本人 GitLab 令牌、写本机配置 `~/.hwf/feishu-bridge.local.json`，然后在一个 Herdr 窗格里常驻 `python3 ~/.hwf/scripts/feishu-bridge.py`。完整步骤、用法和排查见 Wiki [飞书接入](https://git.bootingai.net/ai-hub/skills/workflow/team-up/-/wikis/飞书接入)。

## 怎么改

1. 在 `~/.hwf` 里建分支：`git switch -c <改什么>`。
2. 改完跑测试：`python3 -m unittest discover -s tests -p 'test_*.py'`。
3. 推送并开 MR，合并后其他电脑 `git -C ~/.hwf pull` 即生效（改了 `herdr/config.yaml` 且不是本人电脑的，再跑一次 `install.sh`）。

改动立即对本机生效：正在跑的调度者下一次读协议时就会用新规则。
