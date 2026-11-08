#!/usr/bin/env python3
"""给调度者所在的 tab 起一个带 issue 编号的名字，方便在 Herdr 里一眼认出是哪个交付。

    python3 ~/.hwf/scripts/name-tab.py <issue 编号|-> <短标题>

结果形如 `#453 调度 水印开关`；还没有 issue 时编号传 `-`，得到 `待建issue 调度 <短标题>`。
入口新开的 tab 默认都叫 hwf-agent，多个调度者分不清（2026-11-08 用户指示）。
调度者登记后、换了 issue（新建实现 issue、被复用接新需求）时都要调用；member-start.py 在 tab 没按此命名时拒绝领取成员。
stdout = 新名字；exit 1 = 失败（原因写 stderr）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys
from typing import Sequence

_SPEC = importlib.util.spec_from_file_location("hwf_start_scheduler", Path(__file__).with_name("start-scheduler.py"))
assert _SPEC is not None and _SPEC.loader is not None
_START = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_START)

TITLE_MAX = 12  # 标题太长会把 tab 栏挤爆
NO_ISSUE = "待建issue"
LABEL_RE = re.compile(rf"^(#\d+|{NO_ISSUE}) 调度( |$)")


def label_for(issue: str, title: str) -> str:
    issue = issue.strip().lstrip("#")
    if issue == "-":
        head = NO_ISSUE
    elif issue.isdigit():
        head = f"#{issue}"
    else:
        raise ValueError(f"issue 编号只能是数字或 -：{issue}")
    title = " ".join(title.split())[:TITLE_MAX]
    return f"{head} 调度 {title}".rstrip()


def issue_of(label: str) -> str:
    """从调度者 tab 名取出编号部分（#453 / 待建issue），给成员 tab 起名用；不合规返回空串。"""
    match = LABEL_RE.match(label or "")
    return match.group(1) if match else ""


def current_tab() -> str:
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise RuntimeError("不在 Herdr pane 里（没有 HERDR_PANE_ID）")
    return _START.herdr("pane", "get", pane)["result"]["pane"]["tab_id"]


def main(argv: Sequence[str]) -> int:
    if len(argv) < 1:
        print("usage: name-tab.py <issue 编号|-> <短标题>", file=sys.stderr)
        return 1
    try:
        label = label_for(argv[0], " ".join(argv[1:]))
        _START.herdr("tab", "rename", current_tab(), label)
        print(label)
        return 0
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"name-tab failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
