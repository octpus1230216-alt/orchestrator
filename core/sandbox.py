"""orchestrator.core.sandbox
隔离环境：每次运行独立 git worktree + 分支 feat/<task-id>。

- 路径记入 state.json.worktree；失败可 `git worktree remove` 干净回滚
- 主工作区永不被 agent 直接污染
- 非 git 环境（或创建失败）降级为普通目录并警告，保证流程可跑通
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any


def _timeout(config: dict[str, Any] | None = None) -> float:
    """命令超时：统一读 runtime.timeout_seconds（审计 R12，硬编码 60 仅作默认）。"""
    runtime = ((config or {}).get("runtime", {}) or {})
    return float(runtime.get("timeout_seconds", 60))


def _git(args: list[str], cwd: Path, timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # 审计 R12：超时转失败返回（rc=124），不向上抛导致流水线崩
        print(f"[sandbox] [warn] git 命令超时（>{timeout:g}s）按失败处理："
              f"{' '.join(args[:2])} …", file=sys.stderr)
        return subprocess.CompletedProcess(["git", *args], 124, stdout="",
                                            stderr=f"timeout after {timeout:g}s")


def _repo_root(cwd: Path, timeout: float = 60) -> Path | None:
    proc = _git(["rev-parse", "--show-toplevel"], cwd, timeout)
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return None


def branch_name(task_id: str, config: dict[str, Any]) -> str:
    """隔离分支名：feat/<task-id>（前缀取 sandbox.branch_prefix）。"""
    prefix = config.get("sandbox", {}).get("branch_prefix", "feat")
    return f"{prefix}/{task_id}"


def create(task_id: str, config: dict[str, Any]) -> Path:
    """为某 task 创建隔离 worktree + 分支，返回工作目录路径（幂等，可 resume）。"""
    sandbox = config.get("sandbox", {})
    work_dir = (Path(sandbox.get("worktree_root", ".orch-worktrees")) / task_id).resolve()
    if work_dir.exists():
        return work_dir  # 断点续跑：沿用已建隔离环境（绝对路径，避免相对 cwd 二次解析）

    if not sandbox.get("enabled", True):
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    repo = _repo_root(Path.cwd(), _timeout(config))
    if repo is not None:
        branch = branch_name(task_id, config)
        base = sandbox.get("base_branch", "main")
        t = _timeout(config)
        for args in (
            ["worktree", "add", str(work_dir.resolve()), "-B", branch, base],
            ["worktree", "add", str(work_dir.resolve()), "-B", branch],  # base 不存在则从 HEAD
        ):
            proc = _git(args, repo, t)
            if proc.returncode == 0:
                return work_dir
        print(f"[sandbox] git worktree 创建失败，降级为普通目录: {proc.stderr.strip()}",
              file=sys.stderr)
    else:
        print(f"[sandbox] 当前目录不是 git 仓库，降级为普通目录: {work_dir}", file=sys.stderr)

    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def destroy(task_id: str, config: dict[str, Any],
            work_dir: Path | None = None) -> bool:
    """清理隔离环境：git worktree remove + 按 sandbox.cleanup 决定是否删分支。

    审计 R6 接线说明：
    - work_dir 由调用方传 state.worktree 绝对路径（优先）；不传则回退
      config.worktree_root/task_id 重建（依赖 cwd，仅作兜底）；
    - 主仓库定位不依赖调用方 cwd：从 worktree 的 .git 文件反查；
    - 优雅降级：目录已不存在/remove 失败只提示不抛异常，返回是否清理干净。
    """
    sandbox = config.get("sandbox", {})
    wd = Path(work_dir) if work_dir else Path(
        sandbox.get("worktree_root", ".orch-worktrees")) / task_id
    if not wd.exists():
        print(f"[sandbox] 隔离工作区不存在（已清理过）: {wd}")
        return True

    repo = _repo_from_worktree(wd, _timeout(config))
    if repo is not None:
        proc = _git(["worktree", "remove", "--force", str(wd)], repo,
                    _timeout(config))
        if proc.returncode != 0:
            print(f"[sandbox] worktree remove 失败（保留目录，不崩）: "
                  f"{(proc.stderr or '').strip()}", file=sys.stderr)
            return False
    else:
        # 非 worktree（降级普通目录）：直接删
        import shutil
        shutil.rmtree(wd, ignore_errors=True)

    if sandbox.get("cleanup") in ("always", "on_merge") and repo is not None:
        _git(["branch", "-D", branch_name(task_id, config)], repo, _timeout(config))
    print(f"[sandbox] 已清理隔离工作区: {wd}")
    return True


def _repo_from_worktree(wd: Path, timeout: float = 60) -> Path | None:
    """从 worktree 目录反查主仓库根（不依赖进程 cwd）。

    linked worktree 的 .git 是文件：`gitdir: <repo>/.git/worktrees/<name>`；
    普通目录/主仓库则直接 rev-parse。失败返回 None。
    """
    gitf = wd / ".git"
    if gitf.is_file():
        try:
            txt = gitf.read_text(encoding="utf-8", errors="replace").strip()
            if txt.startswith("gitdir:"):
                gdir = Path(txt.split(":", 1)[1].strip())
                if not gdir.is_absolute():
                    gdir = wd / gdir
                # <repo>/.git/worktrees/<name> → parents[1]=<repo>/.git → 再取 parent=<repo>
                repo = gdir.parents[1].parent
                if repo.exists():
                    return repo
        except (OSError, IndexError):
            pass
    return _repo_root(wd, timeout) or _repo_root(Path.cwd(), timeout)
