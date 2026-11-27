# 调度、成员与恢复

本文件解决成员归属、派活、收取和恢复时的信息丢失问题。用真实身份、单一写入者和持久记录推进任务，不把界面状态当成交付结果。

新会话与恢复时先读本文件「角色与启动配置」「初始化」「派活与结果收取」「记录与恢复」；并行、评审、前后端协作在对应动作前再读相关章节。用户沟通读 [communication.md](communication.md)，工作树操作读 [worktrees.md](worktrees.md)。

## 角色与启动配置

| 角色 | 职责与交付物 |
|---|---|
| scheduler | 调度、spec、issue/标签、阶段记录、证据索引、核验与通知；不代做成员工作 |
| arch | 复杂度判定、关键决定、步骤拆分、独立方案评审、严重争议裁决、代码风险终审、最终验收；不写方案 |
| arch-lite | 方案初稿、逐文件改动、回滚验证表与回改；不签发、不拍板 |
| precheck | 机械取证、候选问题和严重度建议；交证据表，不给通过结论 |
| code-review | 用例终审、完整代码评审、常规回改复核、实现前核验；独立于作者 |
| test-writer | 按确认的验收条件编写/回改用例、提前准备脚本、补步骤的定向断言 |
| test | 正常流程评审放行后、debug 在 develop 部署后的真实自动化、UI 走查、报告与录屏/截图证据 |
| dev-fe / dev-be / dev | 分别只写前端、后端或不分端的仓库，按已批准验收与用例实现、修复 |

- 成员执行体、模型、thinking、YOLO 启动参数统一维护在全局配置 `roles.yaml`（入库团队默认）与本机覆盖 `roles.local.yaml`（不入库，只需写要覆盖的角色；换 `tool` 整条替换，同 `tool` 或省略 `tool` 逐字段覆盖；`scripts/role_config.py sync` 分别同步入库 `herdr/config.yaml` 与不入库 `herdr/config.local.yaml` 供 Herdr 插件入口读取），读取顺序为全局配置（优先 `roles.yaml` + `roles.local.yaml`，回退 `herdr plugin config-dir herdr-workflows`）→ 仓库 `.hwf/config.yaml` → `.hwf/config.local.yaml`，后者按整条命名 entry 覆盖。同机同角色仅维护全局配置，不在各业务仓库重复定义。
- scheduler 跟随发起会话的模型与 thinking，配置不写死；其余成员必须显式使用配置的模型参数，不能依赖执行体默认值。访问 herdr socket 的验证用 `test-herdr` profile。debug 的开发角色见 [implementation.md](implementation.md)「debug 模式」。
- 用户指定其他模型才可覆盖，记录来源和适用范围；模型不可用/额度耗尽须报告阻塞，取得本次替换授权后用 `--model-override` 与 `--authorized-by` 领取。授权结束恢复默认，因额度临时替换的按授权在额度恢复后切回。替换不取消独立终审。
- 成员统一以 YOLO 权限启动，但不豁免全局禁令与授权边界。模型、执行体或 herdr 版本变化后，首次使用 profile 前实测 `working`/`blocked` 识别及结论格式；换调度者执行体时同样核验审批界面，不能仅凭 working 可识别就启用。
- scheduler 不做方案、开发、测试、评审、取证和浏览器操作；只派 Herdr 成员，不用 subagent、临时 CLI Agent 或子代理监听。误用的结果不作交付证据，停止后重新派对应成员，在 STATE 记明。
- 成员与用户的单一沟通出口见 [communication.md](communication.md)「沟通边界」。

## 初始化

1. 核对 `HERDR_ENV=1`，不在 Herdr pane 内停止控制；读 `herdr --skill`、`--help` 和实际要用的命令组，不执行会改布局的默认命令探语法。
2. 按主入口阅读表读必读规则，取得 `herdr pane current --current` 的真实 pane 后才登记；不得把 rename 当成收到新入口 prompt 后的首个动作，入口还需确认接收。已登记/复用的调度者只回读身份，不重复改名；失败先查身份，不静默另建调度者。
3. scheduler 名为 `delivery-scheduler-<handle>`：handle 是真实 pane id 小写后将连续非 `[a-z0-9]` 替换为 `-`、去掉首尾 `-`。名称须匹配 `[a-z][a-z0-9_-]{0,31}`，超过 32 字符报阻塞，不截断、不据 handle 反推 pane。
4. 用 `agent get` 回读 `name + pane_id + session id` 三元组并登记 STATE。任一字段变化或 `agent_not_found` 都须停止向旧身份派活，核对归属与交接后再登记；名称随 agent 退出消失，同 pane 重启保留原登记名并更新 session。
5. 运行 `scripts/name-tab.py <issue编号或-> <短标题>` 给自己的 tab 起名；新建 issue 或更换 issue 后同步更新，否则 member-start 拒绝领取。读项目规则、原 issue、关系及全部评论，记录仓库/worktree/分支/HEAD/可写范围。
6. 按「记录与恢复」执行过期清理、匹配 INDEX；命中接续，不命中才建任务目录和索引。项目与 issue 身份用完整 GitLab URL，不仅用 IID。
7. 只按下一阶段所需领取角色，不预建整排空闲成员。写入与工作树授权按 [worktrees.md](worktrees.md)。

## 成员领取与会话

- 只用 `scripts/member-start.py <profile> --task <任务> --cwd <工作树>` 领取，不手写 pane split/agent start 参数。配置缺模型或脚本非零则报阻塞，不绕过；返回 JSON 的身份、args、归属原样记入 STATE。
- 脚本使用 `i<issue编号>-<profile>`，未建 issue 时 `new-<profile>`，占名时加后缀；profile ≤12 字符。按调度 tab 的编号生成，不手工起名；建 issue 后更新 new 名称，旧 `delivery-<profile>-<handle>` 成员不复用。
- 只复用本团队、本 issue 的同角色空闲 pane（优先选 cwd 一致的窗格以沿用会话；若本 issue 同角色空闲窗格在旧目录，`member-start.py` 会先退出旧会话、切换 shell `cwd` 并回读确认后再启动新会话，同时自动回收多余空闲副本）；先收取已有产出、核对 INDEX/STATE 和读屏，再判断空闲。`idle/done` 不证明无在途工作或已交付，`working/blocked/unknown`、启动未就绪及审批界面都不算空闲。已知有在途任务或未收取结果，用 `--exclude` 排除。
- 新建成员前、切换阶段时，按下方「pane 收尾」重新盘点。优先用已有合格成员；不为假设的后续工作囤积窗格。
- 有合格空闲成员就复用，包括本团队其他 tab 中的成员；均忙才新建。成员不跨调度者占用、不抢别的 issue 未释放的成员；创建前记录角色、pane、session 与 `created_by`，各团队无跨调度者角色锁。
- 每个调度者独占 tab；成员只在自己的 tab 内创建/复用，布局、保持焦点、最小窗格由 member-start 管理，太挤另开 tab。记录实际返回的 pane/tab，不拼 ID，不搬动其他团队。
- 派活前保证持久标题为 `角色-实际模型-任务`，未定任务先写“待派任务”，领取、复用或改任务时更新；不只依赖进程动态标题。
- 复用 pane 不等于沿用会话：新 issue（含父转子、子转子）必须新会话。先保存旧产出与 session，确认交接后用 member-start 默认重启，不带 resume/continue；只有能验证 session 已变化时才用 `/new`。同 issue 回改可沿用，但每次阶段切换、收到 `roles.yaml` / `herdr/` 角色配置更新，或发现已建成员的实际执行体/模型与当前生效角色配置（`roles.yaml` 叠加 `roles.local.yaml`）不一致（且无有效 `--model-override` 用户授权）时，派活前必须先运行 `member-start.py --keep-session` 让脚本自动将旧执行体/模型窗格重启到新配置，禁止绕过脚本直接向旧配置会话 `herdr agent prompt`；同一开发会话连续 2 轮真机或集成回改仍未收敛时，去掉 `--keep-session` 换干净新会话。test 每轮新会话按 [review-testing.md](review-testing.md) §8。
- `--keep-session` 只在同一 issue 可沿用且实际执行体与启动命令符合配置时保留原会话（不一致时脚本自动重启切换）。尚无 spec 时按临时需求标识归档，发布后关联 URL，再给正式 issue 建新会话，不恢复临时分析会话冒充。

## 派活与结果收取

### 派活契约

- 派活和回话尽可能精简，把话说清楚就行（2026-11-18 用户要求）。单条 `herdr agent prompt` 命令不超过 2500 字（超长会被记录守卫拦下）：较长背景、验收清单或代码上下文先写入 `<主仓库>/.hwf/tmp/<任务标识>/dispatch-<角色>.md`，派活命令只写本次独有目标、唯一可写范围、对应版本、交付位置、预算与该文件绝对路径。协议、角色规则、howto 和 STATE 里已有的不复述，写章节或路径；内部代号首次出现带一句白话。
- 只给目标、用户原话、上下文、验收边界和交付位置；不编操作步骤、猜排查路线、指定工具参数或复述成员已有全局规则。用户点名工具原样转达，做错时纠正目标而非编更细教程。
- 正式任务附角色、完整 issue URL、spec/方案路径、阶段、worktree/分支/HEAD、唯一可写范围、验收条目、评审剩余轮数及交付路径。交付、留证与测试报告路径必须写带 `<主仓库>/.hwf/tmp/<任务标识>/` 前缀的完整绝对路径（如 `.../testing/<轮次>/`、`.../development/<切片>/`、`.../reviews/<阶段>-r<N>.md`），不得简写成相对仓库根的裸 `testing/...`、`evidence/...`、`reviews/...`；严禁在主工作树或 worktree 根目录新建顶层产物目录，也不得改业务仓库 `.gitignore` 遮盖误放目录。临时只读分析附问题原话、链接、证据位置和结尾格式。成员先读主入口与本角色/阶段指向的规则，不通读所有主题。
- 评审另附 in-scope/out-of-scope、复核项、预算、停止条件，并要求终审先独立列严重项再读 precheck；缺范围契约不得派。用户可见结论的摘要/全文格式按 [communication.md](communication.md)「原文与材料」。
- 开发首次派活的权限边界按 [implementation.md](implementation.md) §6 实现前检查点，逐切片写核验方式（独立核验/自核）；不能用笼统“开发”跳过只写测试的阶段。进入评审或测试后的回改按同节「回改打包」单趟闭环交接（同趟完成回改前红证、代码修改与绿证，不再拆步停等放行）。
- test 验收与冒烟只派验收目标与交付要求，不指定工具或 skill。其他角色派活只写本阶段要用的 skill 名，不复述其内容：test-writer 写用例、code-review 评用例用 `acceptance-test-cases`；dev 留证与 code-review 实现前核验用 `test-first-evidence`；code-review 代码评审与回改核验用 `scoped-code-review`。

### 等待与完成判定

1. `herdr agent prompt <明确target>` 不带 `--wait`；同一轮立即用后台作业运行 `python3 ~/.hwf/scripts/member-wait.py <成员> --repo <主仓库> --task <任务标识>`。不先回复用户再挂等待。回合结束前本团队每个 working/blocked 成员都须有后台等待；进行中交付任务（非对话模式、非待上线/等你处理）且 `待用户事项：无` 时，禁止在零活跃成员、零后台等待的情况下单方面于终端或本地 STATE 写「暂停/收口」就结束回合——能继续的授权工作须当回合派发下一环并挂等待，确需暂停或等用户决策须先按 [communication.md](communication.md)「需要用户决定」走完整阻塞通知流程。不使用前台等待、轮询、sleep 循环或周期性监控替代。禁用裸 `herdr agent wait`，记录守卫会拦下。
2. member-wait 单次最多等 10 分钟，边等边看屏幕。按退出码处理，同一次处理中做完：
   - `0` 到达目标状态：解析 `agent_status`，仍需等待的续挂或派下一环。blocked 用 `--until working --until idle --until done`。
   - `3` 等满 10 分钟仍在干活、屏幕在变：评审类问一次 checkpoint（“还有哪条验收没结论；没有则收敛输出”）后续挂；其他角色直接续挂。checkpoint 只核范围，不要求降严重度或放行，不计轮。**常规退出码 `3` 续挂（及 `member-settle` 的 `finished: no` 续挂）禁止逐次往 `STATE.md` 追加过程流水**，只在派活、收取最终结论或退出码 `4`/`5` 异常换会话时记 STATE。
   - `4` 卡住（报错停留 5 分钟，或 working 但屏幕 15 分钟没变）：脚本已把「⚠ 卡住、正在换新会话」写进 issue 正文。关掉该 pane，用 member-start 开新会话，把原派活内容原样重发，再挂 member-wait；STATE 记旧 session 与原因。不要接着等旧会话。
   - `5` 卡住且需要用户处理（登录、代理、额度，或同一任务 60 分钟内第二次卡住）：脚本已写「等你处理」并发 ntfy。停下等用户，不再自己换会话重试；用户处理后先 `state-set 待用户事项 无`，再重派。
3. 后台 shell 返回假超时或未响应不代表成员完成；一次 `agent get` 核对后原样续挂，不写等待循环。watch-guard 在「成员未挂等待」或「进行中任务零活跃成员且无待用户事项却准备静默停止」时均会拦下回合结束：按提示补齐等待、当回合继续派活，或发 `todo` 评论＋贴 `状态:等你处理`＋发 ntfy＋写清 STATE「待用户事项」后再停；成员确已不属本任务则在 STATE 记录，不猜测忽略。
4. 卡住恢复后，`state-set 当前阶段` 把「⚠ …」那句换成新的实际结果，不留过时警告。
5. 用 `scripts/member-settle.py <成员> <落盘路径>` 收取并核验：读到本轮最终结论，间隔至少 15 秒两次状态均 idle/done 且读屏稳定才算完成。`finished: no` 续挂，不能把 settled、idle 或 prompt 返回当完成。结论与交付物都收齐才算阶段结束。
6. prompt 超时或 `agent_prompt_stalled` 不证明未送达。先核状态与读屏；仅当本次原文仍停在输入行且最后 12 行无审批短语，才可按实际 CLI 语法补一次 Enter。不重发整段 prompt，不重跑入口。
7. blocked 或 idle/done 但显示审批界面时，读屏（含 detection）并按 [communication.md](communication.md) 通知用户具体选项，不代按键、不向 blocked prompt；仅收件箱可在用户明确选项、核对题目未变后转达按键。idle/done 的普通成员提问，在既有授权内可用 prompt 回答，越界才问用户。
8. 方案评审 arch、用例评审 code-review 只读：不执行命令、不写文件，终端输出结论，由调度者用 member-settle 落盘 `reviews/<阶段>-r<N>.md`。终端截断先增大 lines/recent-unwrapped；仍不全让这两类角色分段重发，其他可写角色可交证据文件。不能把 `.screen` 当原始 Markdown。
9. precheck 结尾 `预审完成`；code-review 常规代码评审结尾 `常规评审通过/需修改`，不等于风险终审放行；阶段终审 `通过/需修改`；arch-lite/dev/test `完成/阻塞`。结论对应具体版本。
10. 不启动周期模型监控、不恢复暂停的 member-monitor-health；本轮监听随任务结束收尾，额外定时任务需用户明确要求。外部通知只由调度者发送，HTTP 2xx 不等于用户确认。

## 双层评审与独立性

方案、用例由 precheck 取证后分别由 arch、独立 code-review 终审；代码走 [review-testing.md](review-testing.md) §7，arch 不重复每轮常规全量审查。各阶段上限和出口分别在对应阶段维护。

- precheck 每条候选附 file:line 或原始输出，只能给严重度建议，不能签通过。终审先独立判断，再看证据；严重候选逐条采纳/驳回，报告干净也独立核对接口/退出码、fail closed、分支互斥、状态语义、数据与权限。
- 至少抽查 min(2, 可用依据数)；不足两条从原始交付物补高风险检查点并如实记内容，不编证据。发现一条依据错误则整表作废，由终审全量复核。
- 漏严重项：未签发在本轮扩大复核；已签且未达上限按下一轮计；达上限按该阶段既有严重项触顶路径处理，不重开全面审查。漏检写 STATE。
- precheck 不审自己参与的产物、不兼该 issue 开发者。方案作者与评审者分别领取会话；测试人员不兼被验代码作者。换 profile/模型不是独立，临时换模型也不解除此要求。
- 常规首审与回改核验统一计入所属阶段评审轮次，中途升级 arch 裁定不清零、不增加轮次，不得以“定点复核不占完整轮”绕过阶段上限；仅达上限后核对 arch 唯一最终收口点名项的收尾确认留痕且不新增阻断。不能把代码评审改名 precheck 绕上限；precheck 取证不计轮。
- 每次记录 issue/阶段、交付物版本、证据编号/路径、name/pane/session、实际 model/thinking、轮次、采纳/驳回结果；配置值不能代替实际运行记录。方案/用例/代码分别记录 turns、成本、返工、严重漏检和运行缺陷，无同规模对照不宣称节省比例。

## 并行与前后端协作

### 多调度者并行

- 一个调度者同一时间只负责一个交付，入口只复用无未收尾任务且无未关闭发起 issue 的空闲调度者。误投时用 `task-record.py index-add --queued` 登记新需求（调度者列空），来源 issue 说明，不抢占当前交付。
- INDEX 只改本任务行，转交由新调度者追加自己的行，最后一行代表负责人；收尾状态前缀为 done/delivered/closed/cancelled。主工作树仅放 `.hwf/tmp` 记录，代码在独立 worktree。共同文件/IndexedDB 库升级冲突由后到任务在 issue @用户说明。
- 按 Blocked by 推进；并行前落盘各分支/worktree/唯一写入范围/合并顺序。同一 worktree 或同一文件同时一名写者，共享文件归一个任务或串行，不靠并行跳依赖；各评审测试对应各自已审版本。
- 测试独占服务见 [review-testing.md](review-testing.md) §8。派 test/冒烟前用 `scripts/test-account.py claim --repo <主仓库> --task <任务> --issue <编号> [--candidates <账号…> | --pool-file <池文件>] [--require-tag <标签>] [--exclude-tag <标签>]` 统一领取账号（涉及购买/计费/权限状态时先按 `free`、`paid`、`plus-trial`、`region:cn`、`region:intl` 等套餐标签一步筛出匹配账号，新发现的套餐状态用 `test-account.py tag` 记入本机元数据）；写入派活与 STATE，test 不自行换号。相同 issue 默认只占 1 个账号复测沿用；只有当前步骤确需双席位协作/权限对照时才临时加领第 2 个，且双席位步骤结束、换号或用户指定停用某账号时，必须当场用 `test-account.py release --task <任务> --issue <编号> --account <账号…>` 单独归还闲置账号，不得一直锁到整单结束。
- 退出码 3 表示全被占：排队、不借用抢占，继续其他工作；成员回报或至多 30 分钟后再领取，STATE 写等待原因。其他任务来协调闲置账号时，核对本任务当前实际在用账号后立即用 `--account` 释放未在用的账号。测试与冒烟全部结束且 issue 关闭/登记不上线时按 issue release，交付收尾前按 task release 全部；已收尾任务漏释放由下次 list/claim 自动回收。被挤下线先暂停回报，用 list 查占用，不抢回账号。

### 前后端并行开发

以下为正常流程。debug 同样先明确接口约定和两端范围，由 dev-fe/dev-be 依据 issue 与现有接口核对，歧义由 scheduler 问用户，不派 arch；两端均保留单测先行与实现前核验。后端先合测试分支并等待部署，再部署依赖它的前端；相关部署全部成功后 test 真实验收，通过后各仓只创建干净开发分支 → release MR。开发可并行，不引入下列 §7 评审或 release 合并步骤。

1. 两端都改时先由 arch 在 §0 给接口约定：路径/方法、请求、响应类型与可空性、错误码与语义、旧前端调新后端的兼容行为。写不清先澄清；约定原文进入根 issue 与 STATE，同时交两端。是否同时改两端不单独触发方案/防风险开关。
2. 根 issue 列前后端步骤，各步骤只属一端；前端 Blocked by 后端只阻塞真实浏览器测试与合并，不阻塞开发/评审。是否建子 issue 按 [planning.md](planning.md) §3.1，不额外建 spec。
3. 各仓基于自身 origin/main 建同名 release 汇总分支，dev-fe/dev-be 各自新会话只写本端。测试可按约定 mock 接口，生产不能添加假数据、临时开关或分支先跑起来。
4. 约定行不通先停，由 arch 裁决，scheduler 更新根 issue/STATE 并通知两端；受影响已审部分 code-review 定点复核，沿用原轮数记录，不私改接口。
5. 两端各走 §7 并逐条核对接口，可同一评审者先后看；arch 风险终审可一次看两端。
6. 后端先评审、接口测试（成功及各错误码）、合入测试分支、部署、冒烟、汇入后端 release；前端真实浏览器测试等真实后端部署后再跑，通过后合入测试分支/冒烟/release。依赖新接口或字段的前端不得先合；前端联调发现后端问题时，退回后端按 [review-testing.md](review-testing.md) §8「测试失败后的回改」走单趟修复留证与增量核验（最多 2 轮，不重开首审）、重新部署冒烟并同步后端 release 后再测前端。
7. 上线各仓一个 MR。旧前端兼容时先后端部署再前端；否则明确同时上线要求。上线清单每仓一行，部署步骤在完成汇报，不塞表格。发布其他条件按 [delivery.md](delivery.md)。STATE 分两线记成员、阶段、worktree、分支/HEAD、接口版本和等待关系。

## 记录与恢复

### 任务标识与索引

- 有 issue：项目 path 的 `/` 替为 `-` 再接 IID；无 issue：`req-<本机YYYYMMDD>-<原始输入sha1前8位>`，原文逐字算，不裁剪。
- 初始化读 INDEX 前用 `scripts/clean-tmp.py <仓库根>`：清理最新文件超过 10 天且 INDEX 已收尾、发起 issue 已关闭（或无索引）的 `.hwf/tmp/<任务>` 目录，并跨 `~/.herdr/worktrees/` 全部仓库子目录按六重安全门禁（非在途任务、超过阈值未改动、无窗格占用、无未提交/未跟踪改动、`.hwf/tmp` 无独有证据、HEAD 已在远端分支）自动回收过期陈旧 worktree；在途、未关闭发起 issue、INDEX 不删。脚本非零记 STATE 后继续初始化；目录被清也按 GitLab 恢复同一标识。
- 创建目录后用 `task-record.py index-add` 追加，变化用 index-set，只改自己那行；收尾状态只由 wrapup.py 写。禁止手写/重定向/替换 INDEX。恢复先匹配，不另建 issue、不重置轮次、不覆盖其他任务。

### STATE.md

每次阶段转换增量维护 `.hwf/tmp/<任务>/STATE.md`，不抄完整会话。顶部 `<!-- hwf:top -->` 内五项只用 `task-record.py state-set`：通知模式、当前阶段、下一动作、待用户事项、最近用户通知。其中当前阶段、下一动作、待用户事项会自动同步到根 issue 进度区（`planning.md`「根 issue 维护区」），阶段一变、待用户事项一出现或解除就立刻更新，不攒到步骤完成。正文记录：

- 需求原文与来源、GitLab 所有相关完整 URL、步骤与依赖、登记池/待定清单/通知 note、进度标签；新建入口 run id 用 `$HWF_RUN_ID`，复用入口查 hwf runs 的仓库 team-up 最新项，无则 n/a。
- 用户授权（测试、worktree、提交推送合并等）、判定开关与命中项、需求理解卡路径及 card-check 结果、关键决定原文、会话、确认来源和改判记录。
- 各评审计数、具体产物版本、结论与 reviews 路径；方案/用例 snippet URL/ID/SHA/上传时间/回填位置，裁定、验收映射、测试证据；最终验收轮次/所看SHA/原文/note。
- 成员身份三元组、agent kind、issue、工作目录/分支/HEAD、可写范围、pane created_by；当前在途成员及等待；创建的服务进程/端口/origin、启动停止时间、关闭动作、回读与残留。

STATE 顶部只保留当前情况；当 `STATE.md` 超过约 220 行或 28KB 时，`task-record.py state-set`（或手动 `task-record.py state-compact`）会自动保留来源/授权/基线/关键决定等核心章节与最近轮次记录，将更早的已封板阶段与成员流水无损归档至同目录 `STATE-history.md`。恢复会话默认只读 `STATE.md`，仅在回溯特定早期证据时按需检索 `STATE-history.md`。待上线时“下一动作”写完整 wrapup 命令及发起 issue 关闭限制。恢复读主入口、必读主题、INDEX/STATE 和 GitLab 最新记录，核对活跃成员，再继续；压缩依配置执行，不用会话记忆代替状态。输出较大先落盘后按需读取，同一 help 不重复查。

### pane 收尾

成员结果收齐时就释放，不默认留到整单结束。调度者按本轮实际派活、交付物、会话身份及读屏，把成员分为「正在用 / 下步复用 / 结果待收 / 可关闭」。在 STATE 记录交付物与成员的对应关系；未提交改动也要有工作树、当前状态及接手人。不用空闲时长判断是否可关。

- 留作下步复用，必须写明下一项具体派活。同一任务、角色、工作目录默认最多留一个空闲候补；正在并行执行、等待审批或结果待收的不占候补名额。
- 只关闭自己为本任务创建、已完成交接、无未答问题且没有下一项派活的成员。关闭前再次核对 pane、会话、归属、读屏及是否承载服务；用户、他人、借用的窗格、调度者及身份不明的一律不动。单个释放运行 `python3 ~/.hwf/scripts/member-release.py --issue <编号> --pane <窗格> --session <本轮已交接的会话标识>`；进入 `状态:待上线`（等待用户合并 `main` 或等待跨团队前置上线）或长时外部阻塞等待前，运行 `python3 ~/.hwf/scripts/member-release.py --issue <编号> --all-idle` 一键释放本单全部空闲就绪成员（后续用户回复时再按需重领），STATE 记关闭结果和保留原因。
- 阶段中释放成员只关窗格，不调用整项 wrapup，不清工作树、分支、浏览器、服务或证据。交付结束仍按 [delivery.md](delivery.md) §9 处理剩余资源，目录另按 [worktrees.md](worktrees.md) 核验。
