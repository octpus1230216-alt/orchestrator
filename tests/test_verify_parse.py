"""单测 3：verify 解析（行尾选项 timeout/optional/required + setup/teardown 分段）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verify import _parse_line, parse_acceptance  # noqa: E402


class TestParseLine(unittest.TestCase):

    def test_defaults(self):
        item = _parse_line("python -m pytest -q", 600)
        self.assertEqual(item["command"], "python -m pytest -q")
        self.assertEqual(item["timeout"], 600)
        self.assertTrue(item["required"])

    def test_timeout_and_optional(self):
        item = _parse_line("python x.py   # timeout=30 optional 冒烟，可失败", 600)
        self.assertEqual(item["command"], "python x.py")
        self.assertEqual(item["timeout"], 30)
        self.assertFalse(item["required"])
        self.assertIn("冒烟", item["note"])

    def test_optional_false_still_required(self):
        item = _parse_line("a  # optional=false", 600)
        self.assertTrue(item["required"])

    def test_required_false(self):
        item = _parse_line("a  # required=no", 600)
        self.assertFalse(item["required"])

    def test_hash_inside_command_not_stripped(self):
        """# 前不是空白时不算注释：命令内的 # 保留。"""
        item = _parse_line(r'python -c "x=1#2"', 600)
        self.assertIn("#2", item["command"])


class TestParseAcceptance(unittest.TestCase):

    MD = """\
# 验收标准

## setup
```sh
python -m venv .venv   # 起环境
```

## 检查项（可执行命令）
```sh
python -c "print('ok')"     # timeout=30
python -c "raise SystemExit(1)"  # timeout=10 optional 演示可选项
```
# 这行以 # 开头是注释，忽略

## teardown
```sh
rmdir /s /q .venv
```
"""

    def test_sections(self):
        s = parse_acceptance(self.MD, default_timeout=600)
        self.assertEqual(s["setup"], ["python -m venv .venv"])
        self.assertEqual(s["teardown"], ["rmdir /s /q .venv"])
        self.assertEqual(len(s["check"]), 2)
        first, second = s["check"]
        self.assertEqual(first["command"], "python -c \"print('ok')\"")
        self.assertEqual(first["timeout"], 30)
        self.assertTrue(first["required"])
        self.assertFalse(second["required"])
        self.assertEqual(second["timeout"], 10)

    def test_comment_line_ignored(self):
        s = parse_acceptance("```\n# comment\npython x\n```")
        self.assertEqual([c["command"] for c in s["check"]], ["python x"])

    def test_outside_fence_ignored(self):
        s = parse_acceptance("## 检查项\npython x  # 围栏外的行不算命令")
        self.assertEqual(s["check"], [])


if __name__ == "__main__":
    unittest.main()
