# Issue tracker: GitLab

Issues and PRDs for this repo live in its GitLab project. Use available GitLab tools or the authenticated GitLab API for tracker operations.

## Defaults

- The GitLab host and project are `{{HOST}}` / `{{PROJECT}}` (also recorded in `docs/agents/herdr-project.yaml`). If the target is ambiguous, confirm it with the user before publishing.
- Create issues in that project unless the user explicitly specifies another GitLab project.
- Reuse the source issue's milestone when present. Otherwise leave milestone, assignee, weight, and due date unset unless the user specifies them.
- Resolve labels by exact name among the project's available labels, including inherited group labels. If a canonical label is missing, do not create an alias; report it (re-run `python3 ~/.hwf/scripts/init-project.py <repo> --apply`).
- Read the relevant GitLab issue, linked issues, and comments before updating it or deriving tickets from it.
- If GitLab access is unavailable or the target project cannot be verified, stop before publishing and ask the user to provide the required access or confirm the project.

## Conventions

- **Create or update an issue**: use the GitLab issue create or update operation with a title and Markdown description. Address an existing issue by project and issue IID.
- **Labels**: 本仓库只有 9 个标签，见 `docs/agents/triage-labels.md`。spec/PRD 是根 issue，打 `需求`；实现子 issue 打 `子任务`；每张 open 的 issue 恰好一个 `状态:` 标签。
- **少开 issue**：上线清单、评审残留、既有缺陷、测试债、与需求无关的问题不单独开 issue，规则见 `~/.agents/rules/topics/09-issue-writing.md`「少开 issue 铁律」。
- **Spec and ticket skills**: when available, use `/to-spec` to write and publish the spec, and `/to-tickets` to split it into implementation tickets.
- **Read an issue**: retrieve the issue and its relations, then retrieve its comments when the workflow depends on prior discussion.
- **List issues**: query GitLab issues with the narrowest applicable project, label, state, milestone, or assignee filters.
- **Comment on an issue**: create a GitLab issue note.
- **Apply or remove labels**: use exact available label names and preserve unrelated labels.
- **Close an issue**: comment with the completion result, then close it using `state_event=close`.
- **Parent and child issues**: use native issue hierarchy when supported by the GitLab instance and available tools. Otherwise put a `Parent` reference in each child description and maintain a child-ticket list in the parent. A child has at most one parent.
- **Blocking edges**: use native `blocks` or `is_blocked_by` issue links when supported. Otherwise record explicit `Blocked by` references in ticket descriptions; a generic related-issue link does not imply blocking.
- **Readable descriptions**: keep `Parent` and `Blocked by` sections in ticket descriptions. Treat native relations as the source of truth when available; otherwise use the explicit description references. Use full issue URLs for cross-project references.

## When a skill says "publish to the issue tracker"

Create a GitLab issue using the project defaults above.

- `/to-spec` creates the spec as a top-level issue in the default project and applies `需求` and `状态:待开始`.
- `/to-tickets` writes approved slices as numbered steps in the source spec's 「进度」 section by default; only slices that must be developed in parallel become child issues (`子任务` + `状态:待开始`, inheriting the spec's milestone). Blocking edges are recorded in dependency order using the conventions above.
- Do not close or rewrite the source spec while publishing its child tickets. When native hierarchy is unavailable, only update its child-ticket list.

## When a skill says "fetch the relevant ticket"

Retrieve the GitLab issue with its relations and read all of its comments. Follow pagination for comments and relation lists.

## Frontier

An issue is on the agent frontier only when it:

- is open;
- has the `状态:待开始` label;
- has no unresolved blockers;
- has no child issues; and
- has no assignee.

Check native relations or the explicit description references according to the conventions above. The leaf-only rule keeps a decomposed spec from competing with its implementation tickets.

## Wayfinding operations

Used by `/wayfinder`. The map is a single issue with related implementation tickets.

- **Map**: An issue labelled `wayfinder:map`, containing Notes, Decisions-so-far, and Fog.
- **Child ticket**: A GitLab issue under the map whose label is `wayfinder:<type>`, linked using the parent-child conventions above.
- **Blocking**: Record dependencies using the blocking conventions above.
- **Frontier query**: Find the first open leaf child with no unresolved blockers and no assignee.
- **Claim**: Assign the issue to the authenticated GitLab user.
- **Resolve**: Comment with the answer, close the issue, then add a context pointer to the map's Decisions-so-far.
