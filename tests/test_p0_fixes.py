"""P0 审计修复回归测试（R9 / R2 / R4 / R1）。

- R9: run_segments 停下上报用真实 name-status（D 不得被标成 M → delete_file L3 不漏审）；
- R2: .orch/ 不进 files_changed / collect_writes / commit（exclude 三处过滤）；
- R4: confirm 无 TTY 且未 --yes → NonInteractiveError，CLI 退出码 2、不 hang 不 traceback；
- R1: _checkpoint 接线 state 契约字段 fingerprint_mode / doc_id（切 mode 调用方不改）。
"""
from __future__ import annotations

import builtins
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
import git_push  # noqa: E402
from adapters.base import AgentAdapter, RunResult  # noqa: E402
from core import confirm as confirm_mod  # noqa: E402
from core import dispatch  # noqa: E402
from core import fingerprint  # noqa: E402
from core.policy import REQUIRE, WriteOp, decide  # noqa: E402
from core.state import BudgetLimits, TaskState  # noqa: E402

POLICY_CONFIG = {
    "max_retry": 1,
    "policy": {
        "default": "auto",
        "require_approval": ["delete_file", "modify_ci_or_config"],
        "forbidden": ["force_push", "push_main_directly"],
        "record_levels": ["L2"],
    },
}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- R9
class TestR9RealStatusReport(unittest.TestCase):
    """前序段有删除、次段触发 forbidden 停下时：上报必须是真实 D 行且能被判为 delete_file。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "prompt.md").write_text(
            "## SEGMENT 1\n删除旧模块\n\n## SEGMENT 2\n危险操作\n", encoding="utf-8")
        self._orig = dispatch.execute_and_log
        self.results = [
            RunResult(exit_code=0, files_changed=["D\told.py", "M\tsrc/a.py"]),
            RunResult(exit_code=0, write_requests=[
                {"op": "force_push", "path": "", "reason": "覆盖远端"}]),
        ]
        dispatch.execute_and_log = lambda *a, **k: self.results.pop(0)

    def tearDown(self):
        dispatch.execute_and_log = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocked_reports_delete_not_modify(self):
        # yolo 模式：首段 delete 放行推进，次段 force_push 仍被 FORBID 拦下（不依赖 TTY）
        summary = dispatch.run_segments(self.tmp, None, POLICY_CONFIG, self.tmp,
                                        mode="yolo", state=None)
        self.assertTrue(summary["blocked"])
        self.assertEqual(summary["status"], "forbidden")
        # 真实 name-status 行原样上报：D 绝不能被改写成 M
        self.assertIn("D\told.py", summary["files_changed"])
        self.assertNotIn("M\told.py", summary["files_changed"])
        # 漏审回归：上报内容经 collect_writes 后必须识别出 delete_file 并触发 REQUIRE
        writes = dispatch.collect_writes(SimpleNamespace(
            files_changed=summary["files_changed"], write_requests=[]))
        self.assertIn("delete_file", {w.op for w in writes})
        res = decide(writes, POLICY_CONFIG)
        self.assertEqual(res.verdict, REQUIRE)
        self.assertTrue(any(j.write.op == "delete_file" for j in res.pending))


# ---------------------------------------------------------------- R2
class TestR2OrchExcluded(unittest.TestCase):
    """.orch/（编排器内部 prompt 文件）不得混入 files_changed / 写审计 / 提交。"""

    def test_diff_entries_filters_orch(self):
        after = {"?\t.orch/agent_prompt.md", "M\tsrc/a.py", "A\t.orch\\win.md"}
        kept = AgentAdapter._diff_entries(set(), after)
        self.assertEqual(kept, ["M\tsrc/a.py"])

    def test_collect_writes_filters_orch(self):
        result = SimpleNamespace(
            files_changed=["?\t.orch/agent_prompt.md", "A\thello.txt"],
            write_requests=[{"op": "create_file", "path": ".orch/prompt.md"}],
        )
        ops = {w.path for w in dispatch.collect_writes(result)}
        self.assertEqual(ops, {"hello.txt"})

    def test_ensure_committed_excludes_orch_from_commit(self):
        repo = Path(tempfile.mkdtemp())
        try:
            init = _git(["init", "-b", "main"], repo)
            if init.returncode != 0:
                self.skipTest("git 不可用")
            _git(["config", "user.email", "t@t"], repo)
            _git(["config", "user.name", "t"], repo)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            _git(["add", "-A"], repo)
            _git(["commit", "-m", "init"], repo)
            # 模拟 agent 产出 + 编排器内部文件
            (repo / ".orch").mkdir()
            (repo / ".orch" / "agent_prompt.md").write_text("prompt", encoding="utf-8")
            (repo / "hello.txt").write_text("hi\n", encoding="utf-8")

            self.assertTrue(git_push.ensure_committed(repo, "[orch] test"))
            listing = _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).stdout
            self.assertIn("hello.txt", listing)
            self.assertNotIn(".orch", listing, "commit/PR 不得包含 .orch/")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------- R4
class TestR4NonInteractiveConfirm(unittest.TestCase):
    QUESTIONS = [{"id": "q1", "type": "text", "question": "用什么框架？", "default": "fastapi"}]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "questions.json").write_text(
            json.dumps(self.QUESTIONS, ensure_ascii=False), encoding="utf-8")
        self._orig_input = builtins.input

    def _raise_eof(self, *_a):
        raise EOFError

    def tearDown(self):
        builtins.input = self._orig_input
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_eof_raises_friendly_error(self):
        builtins.input = self._raise_eof
        with self.assertRaises(confirm_mod.NonInteractiveError) as ctx:
            confirm_mod.run(self.tmp, {}, assume_yes=False)
        self.assertIn("--yes", str(ctx.exception))

    def test_assume_yes_never_prompts(self):
        answers = confirm_mod.run(self.tmp, {}, assume_yes=True)
        self.assertEqual(answers[0]["answer"], "fastapi")

    def test_cli_exits_code_2_without_traceback(self):
        """集成回归：非 TTY（stdin 立即 EOF）且无 --yes → 退出码 2 + 清晰提示，不 hang。

        进程内调用 cli.main（避免 Windows 子进程管道超时死锁干扰测试）。
        """
        import io
        task = "_p0_r4_reg"
        task_dir = cli.SPECS_DIR / task
        task_dir.mkdir(parents=True, exist_ok=True)
        old_streams = (sys.stdin, sys.stdout, sys.stderr)
        try:
            (task_dir / "questions.json").write_text(
                json.dumps(self.QUESTIONS, ensure_ascii=False), encoding="utf-8")
            (task_dir / "state.json").write_text(json.dumps({
                "task_id": task, "stage": "confirm", "stages_done": ["explore"],
                "segment_index": 0, "segments": [], "worktree": "", "skills_used": [],
                "budget": {"llm_calls": 1, "tokens": 0, "usd": 0.0},
                "fingerprint_mode": "file_bytes", "doc_id": "", "agent": "codex",
                "heal_attempts": 0, "updated_at": 0, "extras": {},
            }), encoding="utf-8")
            sys.stdin = io.StringIO("")          # 任何读输入都立即 EOF
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            rc = cli.main(["run", task])
            err = sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_streams
            shutil.rmtree(task_dir, ignore_errors=True)
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)


# ---------------------------------------------------------------- R1
class TestR1FingerprintWired(unittest.TestCase):
    CFG_BYTES = {"fingerprint": {"mode": "file_bytes", "algorithm": "sha256"}}
    CFG_CONTENT = {"fingerprint": {"mode": "content", "algorithm": "sha256",
                                   "content": {"normalize": ["strip_zip_timestamps"]}}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "context.md").write_text("# 背景\n加限流\n", encoding="utf-8")
        (self.tmp / "prompt.md").write_text("## SEGMENT 1\n实现\n", encoding="utf-8")
        self.st = TaskState(task_id="r1-task")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_checkpoint_fills_contract_fields(self):
        cli._checkpoint(self.tmp, self.st, "compile", BudgetLimits(), self.CFG_BYTES)
        self.assertEqual(self.st.fingerprint_mode, "file_bytes")
        self.assertTrue(self.st.doc_id.startswith("sha256:"))
        self.assertEqual(self.st.doc_id, fingerprint.doc_id(self.tmp, self.CFG_BYTES))
        # state.json 落盘后两字段非空（契约回归）
        saved = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["fingerprint_mode"], "file_bytes")
        self.assertTrue(saved["doc_id"])

    def test_mode_switch_no_caller_change(self):
        """切 mode 只改 config：调用方（_checkpoint）代码零改动，mode 字段随 config 变。"""
        cli._checkpoint(self.tmp, self.st, "compile", BudgetLimits(), self.CFG_BYTES)
        first = self.st.doc_id
        cli._checkpoint(self.tmp, self.st, "compile", BudgetLimits(), self.CFG_CONTENT)
        self.assertEqual(self.st.fingerprint_mode, "content")
        self.assertEqual(self.st.doc_id, fingerprint.doc_id(self.tmp, self.CFG_CONTENT))
        # 普通文本 md：两模式同值（B7 设计——切换不是迁移）
        self.assertEqual(self.st.doc_id, first)

    def test_mode_switch_changes_value_for_zip_single_file(self):
        """"值随之变"发生在 zip 场景：doc_id 聚合只收 md（同值为设计），
        单文件层由 compute_doc_id 展示模式生效且调用方不改。"""
        buf = self.tmp / "doc.docx"
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(zipfile.ZipInfo("a.xml", date_time=(2026, 1, 1, 0, 0, 0)), "<x>v1</x>")
        h_bytes_v1 = fingerprint.compute_doc_id(buf, self.CFG_BYTES)
        h_content_v1 = fingerprint.compute_doc_id(buf, self.CFG_CONTENT)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(zipfile.ZipInfo("a.xml", date_time=(2026, 1, 2, 0, 0, 0)), "<x>v1</x>")
        # 同内容重新导出（仅 zip 时间戳变）：file_bytes 判新，content 稳定——调用方接口不改
        self.assertNotEqual(h_bytes_v1, fingerprint.compute_doc_id(buf, self.CFG_BYTES))
        self.assertEqual(h_content_v1, fingerprint.compute_doc_id(buf, self.CFG_CONTENT))


if __name__ == "__main__":
    unittest.main()
