"""P1 审计修复回归测试（R3 / R5 / R6；R12 见后文追加）。

- R3: LLM 计费回调（无全局状态）：真实 usage 解析 / 缺失估算+警告 / mock 同口径；
      预算门 max_tokens / max_usd 实测触发（rc=3）；
- R5: 分段阈值单源：唯一判定函数读唯一键；grep 级防变种断言（compile/dispatch/config
      不再出现第二键）；
- R6: 清理接线：on_merge 真实 fetch+merge-base 判定（合入→清理、未合入→保留、
      fetch 不可用→不崩）；always 立即清理；destroy 幂等、坏 worktree 降级、
      不依赖进程 cwd；orch clean 兜底入口 rc 语义。
"""
from __future__ import annotations

import builtins
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
import git_push  # noqa: E402
from core import compile as compile_mod  # noqa: E402
from core import dispatch  # noqa: E402
from core import explore as explore_mod  # noqa: E402
from core import sandbox as sandbox_mod  # noqa: E402
from core.llm import LLMClient  # noqa: E402


def _urlopen_returning(payload: dict):
    """构造一个假 urlopen：返回给定 JSON 的响应（context manager）。"""
    body = json.dumps(payload).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        return _Resp(body)

    return _fake


# ---------------------------------------------------------------- R3
class TestR3BillingCallback(unittest.TestCase):
    def test_mock_path_bills_with_same_function(self):
        """mock 走同一记账函数：tokens>0、estimated=True、单价参与 usd 计算。"""
        recs: list[dict] = []
        cfg = {"llm": {"fast": {"model": "mock", "price_per_1k_in": 1.0,
                                "price_per_1k_out": 2.0}}}
        client = LLMClient(cfg, on_cost=recs.append)
        client.chat([{"role": "user", "content": "x" * 400}], tier="fast", purpose="compile")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["llm_calls"], 1)
        self.assertTrue(rec["estimated"])
        self.assertGreater(rec["tokens"], 0)          # 估算口径：400/4=100 in + out
        self.assertGreater(rec["usd"], 0.0)            # 单价参与计算

    def test_openai_usage_parsed_not_estimated(self):
        """真实响应带 usage：精确入账、estimated=False、无 [billing] 警告。"""
        recs: list[dict] = []
        cfg = {"llm": {"fast": {"model": "gpt-x", "api_key": "k",
                                "base_url": "http://x", "price_per_1k_in": 1.0,
                                "price_per_1k_out": 2.0}}}
        payload = {"choices": [{"message": {"content": "ok"}}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        with mock.patch.object(urllib.request, "urlopen", _urlopen_returning(payload)):
            LLMClient(cfg, on_cost=recs.append).chat(
                [{"role": "user", "content": "hi"}], tier="fast")
        rec = recs[0]
        self.assertFalse(rec["estimated"])
        self.assertEqual(rec["tokens"], 150)
        self.assertAlmostEqual(rec["usd"], 0.1 * 1.0 + 0.05 * 2.0, places=6)

    def test_missing_usage_estimates_and_warns(self):
        """兼容端点缺 usage：按字符估算并打 [billing] 警告，不静默按 0。"""
        recs: list[dict] = []
        cfg = {"llm": {"fast": {"model": "gpt-x", "api_key": "k", "base_url": "http://x"}}}
        payload = {"choices": [{"message": {"content": "y" * 800}}]}
        err = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", _urlopen_returning(payload)), \
                redirect_stderr(err):
            LLMClient(cfg, on_cost=recs.append).chat(
                [{"role": "user", "content": "p" * 400}], tier="fast")
        self.assertIn("[billing]", err.getvalue())
        rec = recs[0]
        self.assertTrue(rec["estimated"])
        self.assertEqual(rec["tokens"], 400 // 4 + 800 // 4)

    def test_explore_compile_accept_on_cost(self):
        """explore.run / compile.run 的 on_cost 参数存在（回调透传，非全局账本）。"""
        import inspect
        for fn in (explore_mod.run, compile_mod.run):
            sig = inspect.signature(fn)
            self.assertIn("on_cost", sig.parameters, f"{fn.__module__}.{fn.__name__}")


class TestR3BudgetGateTriggers(unittest.TestCase):
    """预算门实测：极低 max_tokens / max_usd → run rc=3 且打印进度与花费。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.task = "p1-r3-gate"
        self.task_dir = cli.SPECS_DIR / self.task
        self._orig_input = builtins.input
        builtins.input = lambda *_a: (_ for _ in ()).throw(EOFError)

    def tearDown(self):
        builtins.input = self._orig_input
        shutil.rmtree(self.task_dir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config_with(self, runtime: dict, price: bool) -> Path:
        import yaml
        cfg = {
            "version": 1, "agent": "codex", "max_retry": 1,
            "llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}},
            "runtime": runtime,
            "policy": {"default": "auto"},
        }
        if price:
            for tier in cfg["llm"].values():
                tier["price_per_1k_in"] = 1000.0    # 1 USD / token，保证 usd>0
                tier["price_per_1k_out"] = 1000.0
        p = self.tmp / "config.yaml"
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        return p

    def _run_cli(self, cfg_path: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(cfg_path), "run", self.task,
                           "--requirement", "给接口加限流", "--yes", "--mock-llm"])
        return rc, out.getvalue(), err.getvalue()

    def test_max_tokens_gate_pauses_with_rc3(self):
        cfg = self._config_with({"max_tokens": 1}, price=False)
        rc, out, err = self._run_cli(cfg)
        self.assertEqual(rc, 3, f"out={out}\nerr={err}")
        self.assertIn("预算超限", out)
        self.assertIn("tokens", out)                  # 进度与花费打印
        saved = json.loads((self.task_dir / "state.json").read_text(encoding="utf-8"))
        self.assertGreater(saved["budget"]["tokens"], 0)   # 真实累计，不再是 0

    def test_max_usd_gate_pauses_with_rc3(self):
        cfg = self._config_with({"max_usd": 0.0001}, price=True)
        rc, out, err = self._run_cli(cfg)
        self.assertEqual(rc, 3, f"out={out}\nerr={err}")
        self.assertIn("预算超限", out)
        saved = json.loads((self.task_dir / "state.json").read_text(encoding="utf-8"))
        self.assertGreater(saved["budget"]["usd"], 0.0)

    def test_max_usd_without_price_warns_explicitly(self):
        """配了 max_usd 无单价 → 启动明确警告，不静默。"""
        cfg = self._config_with({"max_usd": 5.0}, price=False)
        rc, out, err = self._run_cli(cfg)
        self.assertIn("单价", err)
        self.assertIn("usd 恒 0", err)
        self.assertNotEqual(rc, 3)                    # usd 恒 0，max_usd 不误触


# ---------------------------------------------------------------- R6
def _gitc(args: list[str], cwd: Path):
    """测试用 git 调用（返回 CompletedProcess；失败不抛）。"""
    import subprocess
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _git_run(args: list[str], cwd: Path) -> str:
    p = _gitc(args, cwd)
    assert p.returncode == 0, f"git {args} failed: {p.stderr}"
    return p.stdout


class TestR6Cleanup(unittest.TestCase):
    """R6：真实临时 git 仓库 + worktree + origin remote，进程内调用不 spawn CLI。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        # 中央 remote 仓库（模拟 origin，main 上一个初始提交）
        self.remote = self.tmp / "remote.git"
        self.remote.mkdir()
        _git_run(["init", "-b", "main", "--bare"], self.remote)
        seed = self.tmp / "seed"
        _git_run(["clone", str(self.remote), str(seed)], self.tmp)
        _git_run(["config", "user.email", "t@t"], seed)
        _git_run(["config", "user.name", "t"], seed)
        (seed / "a.txt").write_text("a\n", encoding="utf-8")
        _git_run(["add", "a.txt"], seed)
        _git_run(["commit", "-m", "init"], seed)
        _git_run(["push", "origin", "main"], seed)
        self.main_repo = self.tmp / "main-repo"
        _git_run(["clone", str(self.remote), str(self.main_repo)], self.tmp)
        _git_run(["config", "user.email", "t@t"], self.main_repo)
        _git_run(["config", "user.name", "t"], self.main_repo)
        self._orig_cwd = Path.cwd()

    def tearDown(self):
        import os
        os.chdir(self._orig_cwd)
        for p in self.tmp.rglob(".git"):
            if p.is_dir():
                for f in p.rglob("*"):
                    try:
                        f.chmod(0o644)
                    except OSError:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _worktree_case(self, wt_name: str = "feat-wt"):
        """main_repo 建 feat/x worktree 并各自提交一个可合并的改动。"""
        _git_run(["worktree", "add", str(self.main_repo / wt_name), "-B", "feat/x", "main"],
                 self.main_repo)
        wt = self.main_repo / wt_name
        _git_run(["config", "user.email", "t@t"], wt)
        _git_run(["config", "user.name", "t"], wt)
        (wt / "b.txt").write_text("b\n", encoding="utf-8")
        _git_run(["add", "b.txt"], wt)
        _git_run(["commit", "-m", "feat"], wt)
        _git_run(["push", "origin", "feat/x"], wt)
        return wt

    def _config(self, cleanup: str) -> dict:
        return {"git": {"remote": "origin", "base_branch": "main"},
                "sandbox": {"cleanup": cleanup, "branch_prefix": "feat"},
                "runtime": {"timeout_seconds": 60}}

    def _st(self, wt: Path):
        from core.state import TaskState
        st = TaskState(task_id="x", agent="codex")
        st.worktree = str(wt)
        return st

    # ---- 判定与策略 -------------------------------------------------
    def test_branch_merged_true_after_merge(self):
        """跑完并 merge 后：_branch_merged 判 True，worktree 与临时分支被清理。"""
        wt = self._worktree_case()
        cfg = self._config("on_merge")
        _git_run(["merge", "--no-ff", "-m", "merge feat", "feat/x"], self.main_repo)
        _git_run(["push", "origin", "main"], self.main_repo)
        self.assertIs(cli._branch_merged(wt, cfg), True)
        cli._maybe_cleanup(self._st(wt), cfg)
        self.assertFalse(wt.exists(), "已合入 → on_merge 应清理 worktree")
        self.assertEqual(
            _git_run(["for-each-ref", "refs/heads/feat/x"], self.main_repo).strip(), "",
            "cleanup=on_merge 时应顺带删本地隔离分支")

    def test_branch_not_merged_keeps_worktree(self):
        """未合入：判 False → 保留 worktree 并提示 orch clean，不删。"""
        wt = self._worktree_case()
        cfg = self._config("on_merge")
        self.assertIs(cli._branch_merged(wt, cfg), False)
        out = io.StringIO()
        with redirect_stdout(out):
            cli._maybe_cleanup(self._st(wt), cfg)
        self.assertTrue(wt.exists(), "未合入 → 必须保留 worktree（误删保护）")
        self.assertIn("orch clean", out.getvalue())

    def test_fetch_unavailable_degrades_not_crash(self):
        """remote 不可达：判定三态之 None，stderr 提示，不抛异常、不清理。"""
        wt = self._worktree_case()
        cfg = self._config("on_merge")
        cfg["git"]["remote"] = "nope"
        self.assertIs(cli._branch_merged(wt, cfg), None)
        err = io.StringIO()
        with redirect_stderr(err):
            cli._maybe_cleanup(self._st(wt), cfg)
        self.assertIn("orch clean", err.getvalue())
        self.assertTrue(wt.exists())

    def test_always_cleanup_immediately(self):
        """always：不看合并状态，push 后立即清理。"""
        wt = self._worktree_case()
        cfg = self._config("always")
        cli._maybe_cleanup(self._st(wt), cfg)
        self.assertFalse(wt.exists())

    def test_never_cleanup_keeps_everything(self):
        wt = self._worktree_case()
        cli._maybe_cleanup(self._st(wt), self._config("never"))
        self.assertTrue(wt.exists())

    # ---- destroy 降级 / cwd 独立 / 幂等 -------------------------------
    def test_destroy_plain_dir_no_repo_downgrades(self):
        """坏 worktree（普通目录、非仓库）：降级 rmtree，不抛异常。"""
        outside = self.tmp / "plain-wt"
        outside.mkdir()
        (outside / "f.txt").write_text("f", encoding="utf-8")
        import os
        try:
            os.chdir(self.tmp)   # 进程 cwd 也在仓库外
            ok = sandbox_mod.destroy("x", self._config("always"), work_dir=outside)
        finally:
            os.chdir(self._orig_cwd)
        self.assertTrue(ok)
        self.assertFalse(outside.exists())

    def test_destroy_remove_failure_returns_false_not_crash(self):
        """坏 worktree（被 lock，remove --force 必失败）：返回 False、
        目录保留、不崩。"""
        wt = self._worktree_case("bad-wt")
        _git_run(["worktree", "lock", str(wt)], self.main_repo)
        ok = sandbox_mod.destroy("x", self._config("always"), work_dir=wt)
        self.assertIs(ok, False)
        self.assertTrue(wt.exists(), "remove 失败应保留目录而非崩溃")

    def test_destroy_independent_of_process_cwd(self):
        """cwd 依赖修掉：进程 cwd 切到仓库外，仍能从 .git 文件反查主仓库清理成功。"""
        import os
        wt = self._worktree_case()
        _git_run(["merge", "--no-ff", "-m", "m", "feat/x"], self.main_repo)
        _git_run(["push", "origin", "main"], self.main_repo)
        try:
            os.chdir(self.tmp)   # tmp 本身不是 git 仓库
            self.assertIs(cli._branch_merged(wt, self._config("on_merge")), True)
            cli._maybe_cleanup(self._st(wt), self._config("on_merge"))
        finally:
            os.chdir(self._orig_cwd)
        self.assertFalse(wt.exists())

    # ---- orch clean 兜底入口（handle_clean 进程内） --------------------
    def test_orch_clean_rc_semantics_and_idempotent(self):
        import argparse
        import os
        from core import state as state_mod
        wt = self._worktree_case()
        specs = self.tmp / "specs"
        task_dir = specs / "x"
        task_dir.mkdir(parents=True)
        os.chdir(self.tmp)   # clean 也应在任意 cwd 下可用
        try:
            with mock.patch.object(cli, "SPECS_DIR", specs):
                st = self._st(wt)
                state_mod.save(task_dir, st)
                rc = cli.handle_clean(argparse.Namespace(task="x"), self._config("never"))
                self.assertEqual(rc, 0)
                self.assertFalse(wt.exists())
                # 幂等再清 → 仍 0
                self.assertEqual(
                    cli.handle_clean(argparse.Namespace(task="x"), self._config("never")), 0)
                # 无 state → 2
                self.assertEqual(
                    cli.handle_clean(argparse.Namespace(task="ghost"), self._config("never")), 2)
        finally:
            os.chdir(self._orig_cwd)

    # ---- 接线点存在性（三处静态断言） ---------------------------------
    def test_three_wiring_points_present(self):
        src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("_maybe_cleanup(st, config)"), 2,
                         "调用点：_do_push 成功尾 + handle_status push 完成后")
        self.assertIn('"clean":', src, "main 分发接 clean 子命令")
        self.assertIn("def handle_clean", src)
        self.assertIn("def _branch_merged", src)


# ---------------------------------------------------------------- R5
class TestR5SingleSourceThreshold(unittest.TestCase):
    def test_threshold_single_key_drives_decision(self):
        self.assertEqual(
            dispatch.resolve_segment_mode(5000, {"segment_threshold": 1000}),
            dispatch.SEG_PER)
        self.assertEqual(
            dispatch.resolve_segment_mode(5000, {"segment_threshold": 10000}),
            dispatch.SEG_WHOLE)
        # 无 config 键时取唯一默认常量（不再有第二处默认值）
        self.assertEqual(
            dispatch.resolve_segment_mode(5000, {}), dispatch.SEG_WHOLE)
        # 命令行覆盖仍优先于 config
        self.assertEqual(
            dispatch.resolve_segment_mode(10, {"segment_threshold": 10000},
                                          override="per_segment"),
            dispatch.SEG_PER)

    def test_compile_and_dispatch_consistent_on_same_config(self):
        """同一 config：compile 产出的段数与 dispatch 的喂法由同一阈值源决定。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "context.md").write_text("目标\n", encoding="utf-8")
            cfg = {"llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}},
                   "segment_mode": "auto", "segment_threshold": 10}   # mock prompt > 10 字符
            segments = compile_mod.run(tmp, cfg)
            prompt_len = (tmp / "prompt.md").read_text(encoding="utf-8").__len__()
            mode = dispatch.resolve_segment_mode(prompt_len, cfg)
            self.assertGreater(len(segments), 1)      # mock 产两段
            self.assertEqual(mode, dispatch.SEG_PER)  # 同一阈值 → 逐段，判定一致
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_grep_no_second_threshold_key(self):
        """防变种：第二键 segment_threshold_chars 在代码与配置中零出现。"""
        root = Path(__file__).resolve().parents[1]
        for rel in ("core/compile.py", "core/dispatch.py", "cli.py", "config.yaml"):
            text = (root / rel).read_text(encoding="utf-8")
            # 允许出现在"已删除"说明注释里（含 删除/已删 字样的行）
            for i, line in enumerate(text.splitlines(), 1):
                if "segment_threshold_chars" in line:
                    self.assertTrue("删除" in line or "已删" in line or "#" in line.split(
                        "segment_threshold_chars")[0],
                        f"{rel}:{i} 仍存在第二阈值源引用: {line.strip()}")


# ---------------------------------------------------------------- R12
class TestR12ConfigUnifiedReads(unittest.TestCase):
    """R12：runtime.state_dir / resume / timeout_seconds 接线实测。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.state_dir = self.tmp / "state_here"
        self.task = "p1-r12"
        self._orig_input = builtins.input
        builtins.input = lambda *_a: (_ for _ in ()).throw(EOFError)

    def tearDown(self):
        builtins.input = self._orig_input
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, runtime: dict) -> Path:
        import yaml
        cfg = {"version": 1, "agent": "codex", "max_retry": 1,
               "llm": {"strong": {"model": "mock"}, "fast": {"model": "mock"}},
               "runtime": runtime, "policy": {"default": "auto"}}
        p = self.tmp / "config.yaml"
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        return p

    def _run_cli(self, cfg_path: Path, extra=()) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", str(cfg_path), "run", self.task,
                           "--requirement", "给接口加限流",
                           "--yes", "--mock-llm", "--dry-run", *extra])
        return rc, out.getvalue(), err.getvalue()

    # ---- state_dir ----------------------------------------------
    def test_specs_dir_resolution_rules(self):
        """绝对路径直用；相对路径基于 ROOT；未配置回退 SPECS_DIR 常量。"""
        self.assertEqual(cli._specs_dir({"runtime": {"state_dir": str(self.state_dir)}}),
                         self.state_dir)
        self.assertEqual(cli._specs_dir({"runtime": {"state_dir": "my_specs"}}),
                         cli.ROOT / "my_specs")
        self.assertEqual(cli._specs_dir({}), cli.SPECS_DIR)
        self.assertEqual(cli._specs_dir(None), cli.SPECS_DIR)

    def test_state_dir_drives_run_task_location(self):
        """实测：runtime.state_dir 指到 tmp → 任务目录建在 tmp，不落在默认 specs/。"""
        cfg = self._write_config({"state_dir": str(self.state_dir)})
        rc, out, err = self._run_cli(cfg)
        self.assertEqual(rc, 0, f"out={out}\nerr={err}")
        self.assertTrue((self.state_dir / self.task / "state.json").exists())
        self.assertFalse((cli.SPECS_DIR / self.task).exists(),
                         "state_dir 配了仍落默认目录 = 接线失效")

    # ---- resume ---------------------------------------------------
    def test_resume_false_rejects_silent_resume(self):
        """resume=false + 已有 state 且无 --resume → rc=2 清晰报错；显式 --resume 可继续。"""
        cfg = self._write_config({"state_dir": str(self.state_dir)})   # 缺省 resume=true
        self.assertEqual(self._run_cli(cfg)[0], 0)                     # 首跑建立 state
        cfg2 = self._write_config({"state_dir": str(self.state_dir), "resume": False})
        rc, out, err = self._run_cli(cfg2)
        self.assertEqual(rc, 2, f"out={out}\nerr={err}")
        self.assertIn("resume", err)
        self.assertIn("拒绝静默续跑", err)
        # 显式 --resume 仍放行（已阶段全跳过 → dry-run 正常 0）
        rc, out, err = self._run_cli(cfg2, extra=("--resume",))
        self.assertEqual(rc, 0, f"out={out}\nerr={err}")

    # ---- timeout_seconds -----------------------------------------
    def test_git_push_run_receives_configured_timeout(self):
        """monkeypatch 捕获 kwargs：_run/push_branch 的 timeout 等于配置值。"""
        captured: list = []

        def fake_run(args, **kw):
            captured.append((list(args), kw.get("timeout")))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(git_push.subprocess, "run", fake_run):
            git_push._run(["git", "x"], self.tmp,
                          git_push._timeout({"runtime": {"timeout_seconds": 77}}))
            self.assertEqual(captured[-1][1], 77)
            res = git_push.push_branch(self.tmp, "feat/t",
                                       {"git": {"remote": "origin"},
                                        "runtime": {"timeout_seconds": 33}})
            self.assertEqual(captured[-1][1], 33)
            self.assertTrue(res.pushed)
            # 未配置 → 默认常量（不再是无 timeout）
            git_push._run(["git", "y"], self.tmp, git_push._timeout({}))
            self.assertEqual(captured[-1][1], git_push.DEFAULT_TIMEOUT)

    def test_sandbox_git_receives_configured_timeout(self):
        """sandbox.create 的 git 调用 timeout 来自 runtime.timeout_seconds。"""
        captured: list = []

        def fake_run(args, **kw):
            captured.append(kw.get("timeout"))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a repo")

        cfg = {"runtime": {"timeout_seconds": 45},
               "sandbox": {"enabled": True,
                           "worktree_root": str(self.tmp / "wt")}}
        with mock.patch.object(sandbox_mod.subprocess, "run", fake_run):
            wd = sandbox_mod.create("t1", cfg)
        self.assertTrue(captured, "create 未发起 git 调用")
        self.assertTrue(all(t == 45 for t in captured), captured)
        self.assertEqual(wd, (self.tmp / "wt" / "t1").resolve())  # 降级普通目录仍成立

    def test_git_push_timeout_expired_returns_failure_not_hang(self):
        """故意超时实测（真实慢命令 sleep 5，timeout=1）：转 rc=124 失败返回，
        不崩不 hang（耗时远小于 sleep 时长）。"""
        import time as _time
        err = io.StringIO()
        t0 = _time.monotonic()
        with redirect_stderr(err):
            p = git_push._run([sys.executable, "-c", "import time; time.sleep(5)"],
                              self.tmp, timeout=1)
        elapsed = _time.monotonic() - t0
        self.assertEqual(p.returncode, 124)
        self.assertLess(elapsed, 4.0, f"超时未被捕获，实际耗时 {elapsed:.1f}s")
        self.assertIn("超时", err.getvalue())

    def test_sandbox_timeout_expired_returns_failure_not_crash(self):
        """sandbox._git 捕获 TimeoutExpired → rc=124 失败返回不向上抛。"""
        err = io.StringIO()
        with mock.patch.object(sandbox_mod.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)), \
                redirect_stderr(err):
            p = sandbox_mod._git(["status"], self.tmp, timeout=1)
        self.assertEqual(p.returncode, 124)
        self.assertIn("超时", err.getvalue())

    # ---- 防变种 grep -------------------------------------------
    def test_grep_specs_dir_only_via_config(self):
        """handle_* 全部经 _specs_dir(config)；SPECS_DIR 直拼仅剩 _template 播种一处。"""
        src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("SPECS_DIR /"), 1,
                         "仅 template = SPECS_DIR / '_template' 允许直拼")
        self.assertGreaterEqual(src.count("_specs_dir(config)"), 7,
                                "run/push/status/clean/approve/init 均应经 _specs_dir")


if __name__ == "__main__":
    unittest.main()
