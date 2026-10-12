"""单测 4：policy 判定（auto / require / forbid 三档 + 路径升级 + 聚合取最严）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.policy import (  # noqa: E402
    AUTO,
    FORBID,
    REQUIRE,
    WriteOp,
    decide,
    decide_write,
)

CONFIG = {
    "policy": {
        "default": "auto",
        "require_approval": [
            "modify_public_signature", "change_scope", "modify_schema",
            "add_dependency", "modify_ci_or_config", "delete_file",
            "modify_security_code", "merge_pr",
        ],
        "forbidden": ["force_push", "push_main_directly"],
        "record_levels": ["L2"],
    }
}


class TestDecideWrite(unittest.TestCase):

    def test_l1_internal_auto(self):
        """改内部实现（modify_file 普通路径）→ AUTO L1，不打扰。"""
        j = decide_write(WriteOp("modify_file", "src/services/order.py", "修 bug"), CONFIG)
        self.assertEqual((j.verdict, j.level), (AUTO, "L1"))

    def test_l2_create_file_auto_but_recorded(self):
        j = decide_write(WriteOp("create_file", "src/utils/str.py", "新增内部工具"), CONFIG)
        self.assertEqual((j.verdict, j.level), (AUTO, "L2"))

    def test_l3_public_signature_require(self):
        """改公共签名 → REQUIRE（L3 必须问）。"""
        j = decide_write(WriteOp(
            "modify_public_signature", "src/api/user.py", "改返回结构",
            symbol="get_user(uid) -> User"), CONFIG)
        self.assertEqual((j.verdict, j.level, j.trigger),
                         (REQUIRE, "L3", "modify_public_signature"))

    def test_l3_delete_file_require(self):
        j = decide_write(WriteOp("delete_file", "src/old.py", "清理"), CONFIG)
        self.assertEqual((j.verdict, j.trigger), (REQUIRE, "delete_file"))

    def test_forbidden_op(self):
        """force push 任何模式都拒绝。"""
        j = decide_write(WriteOp("force_push", "", "覆盖远端"), CONFIG)
        self.assertEqual(j.verdict, FORBID)

    def test_forbidden_alias_in_text(self):
        """自然语言命中 alias：即便 op 是普通 push_branch 也 FORBID。"""
        j = decide_write(WriteOp("push_branch", "", "请 force push 到远端"), CONFIG)
        self.assertEqual((j.verdict, j.trigger), (FORBID, "force_push"))

    def test_direct_push_main_alias(self):
        j = decide_write(WriteOp("push", "main", "直推 main 快速上线"), CONFIG)
        self.assertEqual(j.verdict, FORBID)

    def test_path_escalation(self):
        """规则 4：普通 modify_file 命中 CI/config/lock/Dockerfile 路径 → 强制 REQUIRE。"""
        for p in (".github/workflows/ci.yml", "config.yaml", "uv.lock",
                  "Dockerfile", "deploy/secrets.env", "config/db.yaml"):
            j = decide_write(WriteOp("modify_file", p, "顺手改"), CONFIG)
            self.assertEqual(j.verdict, REQUIRE, f"{p} 应路径升级")
            self.assertEqual(j.trigger, "modify_ci_or_config")

    def test_plain_config_dir_file_not_over_escalated(self):
        """普通业务源码不受路径升级影响。"""
        j = decide_write(WriteOp("modify_file", "src/config_loader.py", "读配置"), CONFIG)
        self.assertEqual(j.verdict, AUTO)


class TestAggregate(unittest.TestCase):

    def test_worst_verdict_wins(self):
        res = decide([
            WriteOp("modify_file", "a.py"),
            WriteOp("delete_file", "b.py"),
        ], CONFIG)
        self.assertEqual(res.verdict, REQUIRE)
        self.assertEqual(len(res.pending), 1)

    def test_forbidden_beats_everything(self):
        res = decide([
            WriteOp("modify_file", "a.py"),
            WriteOp("force_push", ""),
        ], CONFIG)
        self.assertEqual(res.verdict, FORBID)
        self.assertEqual(len(res.forbidden), 1)

    def test_all_auto(self):
        res = decide([WriteOp("modify_file", "a.py"),
                      WriteOp("create_file", "b.py")], CONFIG)
        self.assertEqual(res.verdict, AUTO)
        self.assertEqual(len(res.l2), 1)


if __name__ == "__main__":
    unittest.main()
