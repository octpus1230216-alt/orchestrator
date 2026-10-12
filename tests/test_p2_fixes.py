"""P2 审计修复回归测试（R7 / R8 / R10 / R11 / R14；R13 在 tests/e2e_stub.py 内 E2E-8/9）。

- R7: verify_parallel 预留字段不静默——true → verify 阶段 stderr 明确提示"预留/串行"；
      false/缺省 → 不打（不误报已并行）；grep 断言 verify.py/heal.py 零读取。
- R8: adapter type 真分派——缺省 cli 兼容；manual 不 spawn 给人工步骤（rc=4）；
      http 明确 NotImplementedError（dispatch/verify 路径均收为 rc=4）；未知值加载即抛。
- R10: orch approve help 文案与实现一致（查看/指引，不承诺"处理"）。
- R11: compile docstring 与实现对齐——行为证明 run 唯一产出是 prompt.md。
- R14: 无 gh 回退给可操作步骤；GitHub 形态不丢 compare 链接。
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
import git_push  # noqa: E402
from adapters import base as base_mod  # noqa: E402
from adapters.base import AgentAdapter  # noqa: E402
from core import compile as compile_mod  # noqa: E402
from core.state import TaskState, mark_done, save as save_state  # noqa: E402


def _config(tmp: Path, *, verify_parallel: bool, adapter_type: str,
             runtime_extra: dict | None = None) -> dict:
    """共用最小 config：trae 名义模块（真实存在薄层），type 由参数决定。"""
    runtime = {"state_dir": str(tmp / "specs"),
               "max_llm_calls": 1000, "max_tokens": 10_000_000}
    runtime.update(runtime_extra or {})
    return {
        "version": 1, "agent": "trae", "max_retry": 1,
        "llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}},
        "adapters": {"trae": {"type": adapter_type, "command_template": "",
                              "install_hint": "stub", "capabilities": {}}},
        "runtime": runtime,
        "sandbox": {"enabled": True, "worktree_root": str(tmp / "wt"),
                    "branch_prefix": "feat", "base_branch": "main"},
        "policy": {"default": "auto"},
        "verify_parallel": verify_parallel,
    }


def _premake_task(tmp: Path, task: str, done: list[str]) -> Path:
    """在 tmp/specs 预建任务目录与 state（续跑直达目标阶段）。"""
    import yaml
    task_dir = tmp / "specs" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "prompt.md").write_text("## TASK\n做点什么\n", encoding="utf-8")
    (task_dir / "acceptance.md").write_text(
        "# 验收\n```sh\nexit 1\n```\n", encoding="utf-8")  # 必失败 → 走 heal → adapter.run
    st = TaskState(task_id=task, agent="trae")
    for s in done:
        mark_done(st, s)
    st.worktree = str(tmp / "wt" / task)
    (tmp / "wt" / task).mkdir(parents=True, exist_ok=True)  # 续跑 verify 时 worktree 应存在
    save_state(task_dir, st)
    cfg_path = tmp / "config.yaml"
    return task_dir, cfg_path


class _CliRunMixin:
    def _run_resume(self, cfg_dict: dict, cfg_path: Path, task: str) -> tuple[int, str, str]:
        import yaml
        cfg_path.write_text(yaml.safe_dump(cfg_dict, allow_unicode=True), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(cfg_path), "run", task, "--resume"])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- R7
class TestR7VerifyParallelReserved(_CliRunMixin, unittest.TestCase):
    def _to_verify(self, verify_parallel: bool) -> tuple[int, str]:
        cfg = _config(self.tmp, verify_parallel=verify_parallel, adapter_type="http")
        task = "p2-r7"
        _, cfg_path = _premake_task(self.tmp, task,
                                    ["explore", "confirm", "compile", "dispatch"])
        rc, _out, err = self._run_resume(cfg, cfg_path, task)
        return rc, err

    def test_true_prints_reserved_notice_not_silent(self):
        """verify_parallel=true → 明确提示预留+串行（不静默忽略）。"""
        rc, err = self._to_verify(True)
        self.assertIn("预留", err)
        self.assertIn("串行", err)
        self.assertIn("暂不生效", err)

    def test_false_or_missing_no_misleading_claim(self):
        """缺省/false → 不打提示，也不出现"并行执行"类误导字样。"""
        rc, err = self._to_verify(False)
        self.assertNotIn("预留", err)
        self.assertNotIn("并行执行", err)

    def test_grep_verify_heal_never_read_verify_parallel(self):
        """防假实现：verify_parallel 在 verify.py / heal.py 中零读取。"""
        root = Path(__file__).resolve().parents[1]
        for rel in ("core/verify.py", "core/heal.py"):
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("verify_parallel", text,
                             f"{rel} 不应读取 verify_parallel（本批恒串行）")


# ---------------------------------------------------------------- R8
class TestR8TypeDispatch(_CliRunMixin, unittest.TestCase):
    def test_default_type_is_cli(self):
        """缺省 type=cli：现有配置零改动保持现行为（兼容承诺）。"""
        a = AgentAdapter({"command_template": "x"})
        self.assertEqual(a.adapter_type, "cli")
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                             .read_text(encoding="utf-8"))
        self.assertNotIn("type", cfg["adapters"]["codex"])       # codex 不加，验证缺省兼容
        self.assertEqual(cfg["adapters"]["trae"].get("type"), "manual")  # 事实归位

    def test_manual_raises_with_human_steps_and_zero_spawn(self):
        """manual → 不 spawn 任何子进程；消息给可操作人工步骤。"""
        a = AgentAdapter({"type": "manual", "command_template": "x"})
        a.name = "trae"
        with mock.patch.object(base_mod.subprocess, "run",
                               side_effect=AssertionError("manual 不该 spawn")) as pr:
            with self.assertRaises(base_mod.AgentUnavailableError) as cm:
                a.run("prompt", cwd=self.tmp)
        pr.assert_not_called()
        msg = str(cm.exception)
        self.assertIn("人工", msg)
        self.assertIn("agent_prompt.md", msg)     # prompt 文件路径
        self.assertIn("--resume", msg)            # 续跑指引
        self.assertIn("无法自动执行", msg)          # 与"不可用"文案区分（裁定1）

    def test_http_raises_not_implemented(self):
        a = AgentAdapter({"type": "http", "command_template": "x"})
        with self.assertRaises(NotImplementedError) as cm:
            a.run("prompt", cwd=self.tmp)
        self.assertIn("暂不支持 http", str(cm.exception))

    def test_unknown_type_raises_at_load(self):
        """未知值加载即抛（构造即报错），绝不静默当 cli 跑。"""
        with self.assertRaises(base_mod.AgentUnavailableError):
            AgentAdapter({"type": "soap", "command_template": "x"})

    def test_cli_http_via_dispatch_rc4(self):
        """全链：http 型在 dispatch 执行时报错 → rc=4 + 清晰打印（无 traceback）。"""
        cfg = _config(self.tmp, verify_parallel=False, adapter_type="http")
        task = "p2-r8-http"
        _, cfg_path = _premake_task(self.tmp, task, ["explore", "confirm", "compile"])
        rc, out, err = self._run_resume(cfg, cfg_path, task)
        self.assertEqual(rc, 4, f"out={out}\nerr={err}")
        self.assertIn("暂不支持 http", err)

    def test_cli_http_via_verify_heal_rc4(self):
        """全链：dispatch 已完成时 http 在 verify/heal 路径也被收为 rc=4。"""
        cfg = _config(self.tmp, verify_parallel=False, adapter_type="http")
        task = "p2-r8-http-v"
        _, cfg_path = _premake_task(self.tmp, task,
                                    ["explore", "confirm", "compile", "dispatch"])
        rc, out, err = self._run_resume(cfg, cfg_path, task)
        self.assertEqual(rc, 4, f"out={out}\nerr={err}")
        self.assertIn("暂不支持 http", err)

    def test_grep_base_actually_dispatches_on_type(self):
        src = (Path(__file__).resolve().parents[1] / "adapters" / "base.py")\
            .read_text(encoding="utf-8")
        self.assertIn('self.adapter_type == "manual"', src)
        self.assertIn('self.adapter_type == "http"', src)


# ---------------------------------------------------------------- R10
class TestR10ApproveHelp(unittest.TestCase):
    def test_approve_help_matches_behavior(self):
        """help 文案=查看/指引，不再承诺"处理待审批"；handle_approve 零改动。"""
        parser = cli.build_parser()
        sub = [a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction)][0]
        helps = {ac.dest: ac.help for ac in sub._choices_actions}
        self.assertIn("查看", helps["approve"])
        self.assertNotIn("处理待", helps["approve"])
        self.assertIn("--resume --interactive", helps["approve"])  # 重审路径在文案里


# ---------------------------------------------------------------- R11
class TestR11CompileDocstring(unittest.TestCase):
    def test_run_produces_only_prompt_md(self):
        """行为证明：预置三输入 + acceptance.md，run 后文件增量恰好 {prompt.md}。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "context.md").write_text("目标\n", encoding="utf-8")
            (tmp / "answers.json").write_text("{}", encoding="utf-8")
            (tmp / "skills.md").write_text("技能\n", encoding="utf-8")
            acc = tmp / "acceptance.md"
            acc.write_text("验收标准（人工维护）\n", encoding="utf-8")
            before = {p.name for p in tmp.iterdir()}
            acc_bytes = acc.read_bytes()
            cfg = {"llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}}}
            compile_mod.run(tmp, cfg)
            after = {p.name for p in tmp.iterdir()}
            self.assertEqual(after - before, {"prompt.md"},
                             f"compile 唯一产出应为 prompt.md，实际新增 {after - before}")
            self.assertEqual(acc.read_bytes(), acc_bytes, "acceptance.md 不应被 compile 改写")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_docstring_no_longer_claims_acceptance_output(self):
        doc = compile_mod.__doc__ or ""
        self.assertIn("prompt.md", doc)
        self.assertNotIn("生成 specs/<task-id>/ 下的产物", doc)   # 旧虚假文案已移除
        self.assertIn("唯一产出", doc)
        self.assertIn("任务**输入**", doc)                        # acceptance 定性为输入


# ---------------------------------------------------------------- R13（产品侧静态断言；主证据在 e2e E2E-8/9）
class TestR13CleanupContract(unittest.TestCase):
    def test_e2e_stub_has_residue_assertions(self):
        src = (Path(__file__).resolve().parent / "e2e_stub.py").read_text(encoding="utf-8")
        self.assertIn("E2E-8(R13)", src)   # worktree 根：空则自愈删、非空 FAIL
        self.assertIn("E2E-9(R13)", src)   # specs 仅剩白名单
        self.assertIn("wt_root.rmdir()", src)

    def test_sandbox_destroy_not_touch_shared_root(self):
        """destroy 契约只删 task 子目录，不越权删共用根（裁定3）。"""
        import inspect
        from core import sandbox as sandbox_mod
        src = inspect.getsource(sandbox_mod.destroy)
        self.assertNotIn("rmtree(wd.parent", src)


# ---------------------------------------------------------------- R14
class TestR14PrGuidance(unittest.TestCase):
    def test_local_remote_note_has_actionable_steps(self):
        """无 gh + 本地裸仓库 origin → note 含分支名/目标/可操作步骤。"""
        tmp = Path(tempfile.mkdtemp()).resolve()
        try:
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)],
                           capture_output=True, check=True, text=True)
            bare = tmp / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)],
                           capture_output=True, check=True, text=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
                           capture_output=True, check=True, text=True)
            cfg = {"git": {"remote": "origin"}, "runtime": {"timeout_seconds": 30}}
            with mock.patch.object(git_push.shutil, "which", lambda _x: None):
                res = git_push.open_pr(repo, "feat/p2-r14", "main", cfg,
                                       title="t", body="b")
            self.assertEqual(res.pr_url, "")            # 本地路径不构造假链接
            note = res.note
            self.assertIn("feat/p2-r14", note)          # 分支名
            self.assertIn("main", note)                 # 目标分支
            self.assertIn("merge --no-ff", note)        # 本地裁定步骤
            self.assertIn("PR/MR", note)                # 托管站手动路径
        finally:
            for p in repo.rglob(".git"):                # Windows .git 只读属性清理
                if p.is_dir():
                    for f in p.rglob("*"):
                        try:
                            f.chmod(0o644)
                        except OSError:
                            pass
            shutil.rmtree(tmp, ignore_errors=True)

    def test_github_remote_keeps_compare_url(self):
        """GitHub 形态远端：pr_url 仍是完整 compare 链接（回退增强不丢链接）。"""
        cfg = {"git": {"remote": "origin"}, "runtime": {"timeout_seconds": 30}}
        with mock.patch.object(git_push.shutil, "which", lambda _x: None), \
                mock.patch.object(git_push, "_repo_web_url",
                                  lambda wd, t=None: "https://github.com/o/r"):
            res = git_push.open_pr(Path("."), "feat/x", "main", cfg, title="t", body="b")
        self.assertEqual(res.pr_url,
                         "https://github.com/o/r/compare/main...feat/x?expand=1")
        self.assertIn("merge_requests", res.note)       # GitLab 构造规则一行提示


if __name__ == "__main__":
    unittest.main()
