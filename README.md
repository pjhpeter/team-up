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
| `member-omp.yml` | 成员的 omp 上下文压缩设置（约 20 万词元压缩） |
| `scheduler-omp.yml` | 调度者的上下文压缩设置 |
| `scripts/` | 收件箱、飞书桥、找调度者、按角色领取成员、给调度者 tab 起名、成员收尾、清理临时目录、新项目接入等脚本 |
| `templates/project/` | 新项目接入时写进仓库的文件模板 |
| `tests/` | 脚本的单元测试，CI 每次推送都跑 |
| `gitlab-inbox.config.json` | 收件箱共用配置：盯哪些 GitLab 项目、只认谁的操作、开关标签、轮询间隔；`proactive` 段是主动接活用的模型 |
| `proactive-rules.md` | 主动接活的规则：什么情况 → 做什么，每条规则单独一档 |

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

群里给机器人下指令时，每句话（包括话题里的回复）都要 @机器人；私聊不用。白名单外的人找机器人，它只回一句「我是牛马，但你不是我的地主」。

每人接一套：用 lark-cli 建好机器人应用、存一个本人 GitLab 令牌、写本机配置 `~/.hwf/feishu-bridge.local.json`，然后在一个 Herdr 窗格里常驻 `python3 ~/.hwf/scripts/feishu-bridge.py`。完整步骤、用法和排查见 Wiki [飞书接入](https://git.bootingai.net/ai-hub/skills/workflow/team-up/-/wikis/飞书接入)。

## 主动接活（observe 档）

没人叫 Agent 时，收件箱每 10 分钟看一遍 GitLab 新开的 issue（创建满 2 小时仍没人指派的）和 main / release-* 失败的流水线，再加上飞书「观察群」里的对话，按 `proactive-rules.md` 逐条判断 Agent 本来该不该主动接活。observe 档**只记录、不动手**：对 GitLab 只读，不在飞书说话，不启动调度者。

| 要做的事 | 怎么做 |
| --- | --- |
| 看观察结果 | `python3 ~/.hwf/scripts/proactive.py report`（生成报告并打开；每条右侧有标注命令） |
| 标注判得对不对 | `proactive.py mark O-0031 ok`、`mark O-0032 wrong 理由`（理由写「严重」算严重误接）、`mark O-0040 should R2 理由`（漏接） |
| 改了规则验证一下 | `proactive.py replay R2 --days 14`：拿旧事件按新规则重判，对照之前的标注，不改原记录 |
| 加观察群 | 机器人要有飞书权限 `im:message.group_msg`；在 `feishu-bridge.local.json` 加 `"observe_chats": {"oc_xxx": "群名"}`，重启飞书桥 |
| 放开一条规则 | 成绩卡显示「可以考虑切 auto」后，由人把该规则的档位改掉（auto 档还没实现，现在按 observe 处理） |

平时不推送；每天 18 点后当天有新的「本来会接」时发一条 ntfy，当天判不了的事件超过 5 个时也发一条。记录都在本机 `state/proactive-*`。观察群里的图片会下载到本机、判断时一起给模型看（每段对话最多 4 张；文件和视频只记名字不下载），聊天原文和图片 7 天后自动删除。

## 怎么改

1. 在 `~/.hwf` 里建分支：`git switch -c <改什么>`。
2. 改完跑测试：`python3 -m unittest discover -s tests -p 'test_*.py'`。
3. 推送并开 MR，合并后其他电脑 `git -C ~/.hwf pull` 即生效（改了 `herdr/config.yaml` 且不是本人电脑的，再跑一次 `install.sh`）。

改动立即对本机生效：正在跑的调度者下一次读协议时就会用新规则。
