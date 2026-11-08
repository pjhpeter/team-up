"""聚焦验证调度者 tab 命名：带 issue 编号，供 member-start.py 识别。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

SPEC = importlib.util.spec_from_file_location("name_tab", Path(__file__).resolve().parents[1] / "scripts/name-tab.py")
assert SPEC is not None and SPEC.loader is not None
NT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NT
SPEC.loader.exec_module(NT)


class LabelTest(unittest.TestCase):
    def test_label(self) -> None:
        self.assertEqual(NT.label_for("#453", "水印开关"), "#453 调度 水印开关")
        self.assertEqual(NT.label_for("453", "  很长很长很长很长的标题超过十二个字  "), "#453 调度 很长很长很长很长的标题超")
        self.assertEqual(NT.label_for("-", ""), "待建issue 调度")
        with self.assertRaises(ValueError):
            NT.label_for("abc", "x")

    def test_issue_of(self) -> None:
        self.assertEqual(NT.issue_of("#453 调度 水印"), "#453")
        self.assertEqual(NT.issue_of("待建issue 调度"), "待建issue")
        for bad in ("hwf-agent", "#453 水印开关v2", "scheduler gpt low", ""):
            self.assertEqual(NT.issue_of(bad), "")


if __name__ == "__main__":
    unittest.main()
