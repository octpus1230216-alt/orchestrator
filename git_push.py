"""orchestrator.git_push
上线推送：验收通过后把隔离分支推到远端并创建 PR。

硬性约束（红线）：永不 force push；永不直推 main，只经 PR 上线。
本模块在执行前会再次校验目标分支，命中 main 即拒绝。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DirectPushForbidden(RuntimeError):
    """试图直推受保护主干（main）时抛出。"""


# 审计 R12：runtime.timeout_seconds 缺失时的默认命令超时（原先完全无 timeout，hang 风险）
DEFAULT_TIMEOUT = 60


def _timeout(config: dict[str, Any] | None = None) -> float:
    """从 config.runtime.timeout_seconds 读命令超时（审计 R12，统一 config 源）。"""
    runtime = ((config or {}).get("runtime", {}) or {})
    return float(runtime.get("timeout_seconds", DEFAULT_TIMEOUT))


@dataclass
class PushResult:
    branch: str
    pushed: bool
    pr_url: str = ""
    note: str = ""


def _guard_base_branch(base: str, config: dict[str, Any]) -> None:
    main_branch = config.get("git", {}).get("main_branch", "main")
    if base == main_branch:
        raise DirectPushForbidden(
            f"禁止直推 {main_branch}：请推送到隔离分支并通过 PR 上线。"
        )


def _run(args: list[str], cwd: Path,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """git/gh 子进程统一入口（审计 R12）：带超时且捕获 TimeoutExpired →
    转成 rc=124 的失败返回（不崩不 hang），由调用方按 returncode 处理。"""
    t = timeout if timeout and timeout > 0 else DEFAULT_TIMEOUT
    try:
        return subprocess.run(args, cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=t)
    except subprocess.TimeoutExpired:
        print(f"[git-push] [warn] 命令超时（>{t:g}s）按失败处理：{' '.join(args[:3])} …",
              file=sys.stderr)
        return subprocess.CompletedProcess(args, 124, stdout="",
                                           stderr=f"timeout after {t:g}s")


def _exclude_orch_dir(work_dir: Path, timeout: float | None = None) -> None:
    """commit 前把 .orch/ 加入仓库的 info/exclude（审计 R2）。

    注意：info/exclude 从 **common dir** 读取（linked worktree 也共用主仓库的
    .git/info/exclude），写到 per-worktree 的 worktrees/<name>/info 下不生效。
    非 git 目录静默跳过（后续 add 也会失败，由调用方统一处理）。
    """
    r = _run(["git", "-C", str(work_dir), "rev-parse", "--git-common-dir"], work_dir, timeout)
    if r.returncode != 0 or not (r.stdout or "").strip():
        # 兼容不支持 --git-common-dir 的旧 git
        r = _run(["git", "-C", str(work_dir), "rev-parse", "--absolute-git-dir"], work_dir, timeout)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return
    git_dir = Path(r.stdout.strip())
    if not git_dir.is_absolute():  # 相对路径基于 work_dir 解析（git -C 语义）
        git_dir = work_dir / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        if ".orch/" not in existing.splitlines():
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if existing and not existing.endswith("\n") else "") + ".orch/\n")
    except OSError as exc:
        print(f"[git-push] [warn] 写 info/exclude 失败：{exc}", file=sys.stderr)


def ensure_committed(work_dir: Path, message: str,
                     config: dict[str, Any] | None = None) -> bool:
    """agent 可能只改文件不提交：push 前在隔离分支上补一次提交。

    返回是否产生了新提交；不碰 main（调用方 worktree 处于 feat/<task-id>）。
    先排除 .orch/，git add -A 不会把编排器内部 prompt 文件带进提交/PR（审计 R2）。
    超时取 runtime.timeout_seconds（审计 R12，超时按失败不 hang）。
    """
    t = _timeout(config)
    _exclude_orch_dir(work_dir, t)
    st = _run(["git", "status", "--porcelain"], work_dir, t)
    if not (st.stdout or "").strip():
        return False
    _run(["git", "add", "-A"], work_dir, t)
    proc = _run(["git", "commit", "-m", message], work_dir, t)
    if proc.returncode != 0:
        print(f"[git-push] [warn] 补提交失败（rc={proc.returncode}）："
              f"{(proc.stderr or proc.stdout).strip()[-200:]}", file=sys.stderr)
        return False
    return True


def _repo_web_url(work_dir: Path, timeout: float | None = None) -> str:
    """由 origin URL 猜 web 地址（git@host:g/u.git / https://host/g/u.git → https://host/g/u）。"""
    proc = _run(["git", "remote", "get-url", "origin"], work_dir, timeout)
    url = (proc.stdout or "").strip()
    if not url:
        return ""
    url = url.removesuffix(".git")
    if url.startswith("https://") or url.startswith("http://"):
        return url
    if url.startswith("git@"):
        host, _, path = url[4:].partition(":")
        return f"https://{host}/{path}"
    return url  # 本地路径（file:// 裸仓库等测试场景）


def push_branch(work_dir: Path, branch: str, config: dict[str, Any]) -> PushResult:
    """把隔离分支推到远端（不触碰 main；非强制推送）。"""
    remote = config.get("git", {}).get("remote", "origin")
    _guard_base_branch(branch, config)
    proc = _run(["git", "push", "-u", remote, branch], work_dir, _timeout(config))
    return PushResult(branch=branch, pushed=proc.returncode == 0,
                      note="" if proc.returncode == 0
                      else f"git push 失败: {(proc.stderr or proc.stdout).strip()[-300:]}")


def open_pr(work_dir: Path, branch: str, base: str, config: dict[str, Any],
            title: str = "", body: str = "") -> PushResult:
    """基于已推送分支创建 PR（默认草稿）；gh 不可用时回退为 compare 链接。

    PR 的目标可以是受保护主干（这不是直推），无需 _guard_base_branch。
    """
    pr_cfg = config.get("git", {}).get("pr", {}) or {}
    title = title or f"{pr_cfg.get('title_prefix', '[orch]')} update {branch}"
    draft = pr_cfg.get("draft_by_default", True)
    if shutil.which("gh"):
        cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body", body or "由 orchestrator 自动创建。"]
        if draft:
            cmd.append("--draft")
        proc = _run(cmd, work_dir, _timeout(config))
        if proc.returncode == 0:
            return PushResult(branch=branch, pushed=True,
                              pr_url=(proc.stdout or "").strip(),
                              note="via gh pr create")
        return PushResult(branch=branch, pushed=True,
                          note=f"gh pr create 失败: {(proc.stderr or '').strip()[-300:]}")
    # 回退：不实现 API 直连，但给可操作步骤（审计 R14），不抛异常，调用方展示 note
    web = _repo_web_url(work_dir, _timeout(config))
    url = ""
    steps = [f"分支 {branch} 已推送（目标 {base}）；未自动建 PR（无 gh CLI 或远端非 GitHub），请人工完成："]
    if web and not web.startswith(("C:", "/", "\\")):
        url = f"{web}/compare/{base}...{branch}?expand=1"
        steps.append(f"  ① 打开 compare 链接一键建 PR：{url}")
        steps.append(f"  ② GitLab 形态则新建 MR：{web}/-/merge_requests/new?"
                     f"merge_request%5Bsource_branch%5D={branch}&"
                     f"merge_request%5Btarget_branch%5D={base}")
    else:
        steps.append("  ① 本地/自建裸仓库无 Web 界面：在本地执行 "
                     f"`git merge --no-ff {branch}` 人工裁定合入（编排器永不直推/代劳 merge），"
                     "再按常规推送主干")
        steps.append(f"  ② 或在托管站手动新建 PR/MR：{branch} → {base}，"
                     "标题前缀取 git.pr.title_prefix")
    return PushResult(branch=branch, pushed=True, pr_url=url,
                      note="\n".join(steps))
