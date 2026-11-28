### Herdr Issue 交付流程

用户启动 `team-up`、要求按 Herdr 团队流程交付需求，或成员领取该流程中的 issue 时，必须先读全局交付协议 `~/.hwf/PROTOCOL.md`（通知与工作树细则按需查阅 `~/.hwf/protocol/communication.md`、`~/.hwf/protocol/worktrees.md`）。项目专属的值（GitLab 项目、登记池、需求看板、测试分支、冒烟地址）读本仓库 `docs/agents/herdr-project.yaml`。任务记录与证据落在本仓库 `.hwf/tmp/<任务标识>/`（已 gitignore）。普通咨询不触发团队或自动化测试。

### Issue tracker

Issues and PRDs are written, published, and tracked in this repository's GitLab project. Before creating, updating, or publishing issues, read `docs/agents/issue-tracker.md`.

### Triage labels

本仓库只有 9 个标签，见 `docs/agents/triage-labels.md`。
