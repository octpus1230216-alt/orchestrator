"""单测 5：approval 无 TTY 行为（CI 遇 REQUIRE 必须 fail，不得 hang）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.approval import handle, has_tty  # noqa: E402
from core.policy import WriteOp, decide  # noqa: E402

CONFIG = {"policy": {"default": "auto",
                     "forbidden": ["force_push", "push_main_directly"],
                     "record_levels": ["L2"]}}

# 一条 L3（delete_file → REQUIRE）+ 一条 L1（modify_file → AUTO）
MIXED = decide([
    WriteOp("modify_file", "src/a.py", "内部修复"),
    WriteOp("delete_file", "src/b.py", "删除旧模块"),
], CONFIG)

FORBIDDEN = decide([WriteOp("force_push", "", "覆盖远端历史")], CONFIG)


def _input_that_hangs(prompt: str) -> str:
    raise AssertionError("无 TTY 路径绝不允许调用 input（会 hang）")


class TestHasTty(unittest.TestCase):

    def test_explicit_override(self):
        self.assertFalse(has_tty(False))
        self.assertTrue(has_tty(True))


class TestNoTtyCi(unittest.TestCase):

    def test_require_without_tty_is_ci_fail(self):
        """CI 无 TTY + auto 模式 + L3 → ci_fail（ok=False），且不读输入、不 hang。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = handle(Path(tmp), MIXED, mode="auto", config=CONFIG,
                         input_func=_input_that_hangs, is_tty=False)
            self.assertEqual((out.ok, out.status), (False, "ci_fail"))
            self.assertTrue(out.pending)                 # 输出待批清单
            self.assertIn("待批清单", out.message)
            log = (Path(tmp) / "approvals.log").read_text(encoding="utf-8")
            self.assertIn("PENDING", log)                # 留痕

    def test_interactive_mode_without_tty_also_fails(self):
        out = handle(Path(tempfile.mkdtemp()), MIXED, mode="interactive",
                     config=CONFIG, input_func=_input_that_hangs, is_tty=False)
        self.assertEqual(out.status, "ci_fail")

    def test_forbidden_written_to_blocked(self):
        """forbidden 任何模式（含无 TTY）→ 拒绝 + blocked.md + 替代建议。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = handle(Path(tmp), FORBIDDEN, mode="auto", config=CONFIG,
                         input_func=_input_that_hangs, is_tty=False)
            self.assertEqual((out.ok, out.status), (False, "forbidden"))
            self.assertIn("不要 force push", out.message)   # 回灌替代建议
            self.assertTrue((Path(tmp) / "blocked.md").exists())


class TestOtherModes(unittest.TestCase):

    def test_pure_auto_passes_without_tty(self):
        """无 L3/forbidden：AUTO 直接放行，与 TTY 无关。"""
        res = decide([WriteOp("modify_file", "src/a.py"),
                      WriteOp("create_file", "src/new.py")], CONFIG)
        out = handle(Path(tempfile.mkdtemp()), res, mode="auto", config=CONFIG,
                     input_func=_input_that_hangs, is_tty=False)
        self.assertEqual((out.ok, out.status), (True, "auto"))

    def test_yolo_passes_l3_with_warning(self):
        out = handle(Path(tempfile.mkdtemp()), MIXED, mode="yolo", config=CONFIG,
                     input_func=_input_that_hangs, is_tty=False)
        self.assertEqual((out.ok, out.status), (True, "yolo_pass"))

    def test_yolo_still_rejects_forbidden(self):
        out = handle(Path(tempfile.mkdtemp()), FORBIDDEN, mode="yolo", config=CONFIG,
                     input_func=_input_that_hangs, is_tty=False)
        self.assertFalse(out.ok)

    def test_tty_approve_and_reject(self):
        """有 TTY：a=approve 通过；r=reject 停止并给出回灌文案。"""
        ok = handle(Path(tempfile.mkdtemp()), MIXED, mode="auto", config=CONFIG,
                    input_func=lambda p: "a", is_tty=True)
        self.assertEqual((ok.ok, ok.status), (True, "approved"))
        bad = handle(Path(tempfile.mkdtemp()), MIXED, mode="auto", config=CONFIG,
                     input_func=lambda p: "r", is_tty=True)
        self.assertEqual((bad.ok, bad.status), (False, "rejected"))


if __name__ == "__main__":
    unittest.main()
