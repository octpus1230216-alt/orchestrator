"""orchestrator.adapters.base
Adapter 规格基类：模板写在 config，adapter 只做变量替换 + subprocess。

class AgentAdapter:
    name: str
    capabilities: dict
    def run(self, prompt, cwd, timeout, policy_hint) -> RunResult
    # RunResult: exit_code, stdout, stderr, files_changed, write_requests

capabilities 键：
    supports_prompt_file / supports_streaming / returns_diff
    supports_write_requests / supports_resume
不满足任务需求时由 dispatch 提示换 agent 或降级。

写操作获取：
- supports_write_requests → parse_write_requests() 解析 agent 输出的写操作请求，交 policy
- 不支持 → 执行前后 `git diff --name-status` 推断改动文件，交 policy
- L1/L2 直接放行；L3 即使事后发现也要补审（dispatch/approval 落实）
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AgentUnavailableError(RuntimeError):
    """agent 无 CLI / 未配置 command_template / 本平台不可用 / type=manual 无法自动执行时抛出。"""


class AgentExecutionError(RuntimeError):
    """subprocess 执行期间错误（超时、启动失败等）。"""


#: capabilities 合法键（用于 config 校验与降级判断）
CAPABILITY_KEYS = (
    "supports_prompt_file",
    "supports_streaming",
    "returns_diff",
    "supports_write_requests",
    "supports_resume",
)


@dataclass
class RunResult:
    """一次 agent 执行的结构化结果（契约见模块 docstring）。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    files_changed: list[str] = field(default_factory=list)      # git diff --name-status 行
    write_requests: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False


class AgentAdapter:
    """所有 coding agent 适配器的基类；子类通常只是薄层（个别才覆写解析钩子）。"""

    name: str = ""
    capabilities: dict[str, bool] = {}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.command_template: str = (cfg.get("command_template") or "").strip()
        self.capabilities = {
            k: bool(cfg.get("capabilities", {}).get(k, False)) for k in CAPABILITY_KEYS
        }
        self.install_hint: str = cfg.get("install_hint", "请安装该 agent 的 CLI 后重试。")
        self.raw_config = cfg
        # 审计 R8：type 真分派（缺省 cli，现有配置零改动保持现行为）；
        # 未知值加载即抛，绝不静默当 cli 跑。
        self.adapter_type: str = (cfg.get("type") or "cli").strip().lower()
        if self.adapter_type not in ("cli", "manual", "http"):
            raise AgentUnavailableError(
                f"agent '{self.name}' 配置了未知 type={self.adapter_type}："
                f"仅支持 cli|manual|http（见 audit-report R8），不会静默当 cli 跑。"
            )

    # ---- capabilities 查询 ----------------------------------------
    def has(self, capability: str) -> bool:
        return self.capabilities.get(capability, False)

    def requires(self, **wanted: bool) -> None:
        """校验任务所需能力；不满足则抛异常提示换 agent 或降级。"""
        missing = [k for k in wanted if self.has(k) != wanted[k]]
        if missing:
            raise AgentUnavailableError(
                f"agent '{self.name}' 能力不满足 {missing}（capabilities={self.capabilities}）；"
                f"请换 agent 或在 dispatch 降级路径执行。"
            )

    # ---- 命令组装（只做变量替换） ----------------------------------
    def build_command(
        self, prompt: str, cwd: Path, policy_hint: str = ""
    ) -> list[str]:
        """按 command_template 组装 argv。支持 {prompt_file} / {prompt_inline}。"""
        if not self.command_template:
            raise AgentUnavailableError(
                f"agent '{self.name}' 未配置 command_template。{self.install_hint}"
            )
        full_prompt = prompt if not policy_hint else f"{prompt}\n\n{policy_hint}"

        # 先拆模板再整 token 替换：避免 shlex 对 Windows 反斜杠路径的转义破坏。
        tokens = shlex.split(self.command_template)
        if "{prompt_file}" in self.command_template:
            pfile = Path(cwd) / ".orch" / "agent_prompt.md"
            pfile.parent.mkdir(parents=True, exist_ok=True)
            pfile.write_text(full_prompt, encoding="utf-8")
            pf_str = str(pfile)
        else:
            pf_str = ""

        argv: list[str] = []
        for tok in tokens:
            if "{prompt_file}" in tok:
                tok = tok.replace("{prompt_file}", pf_str)
            if "{prompt_inline}" in tok:
                tok = tok.replace("{prompt_inline}", full_prompt)
            argv.append(tok)
        return argv

    # ---- 主执行 ----------------------------------------------------
    def run(
        self,
        prompt: str,
        cwd: Path,
        timeout: int | None = None,
        policy_hint: str = "",
    ) -> RunResult:
        """在隔离工作区 cwd 内以 subprocess 执行 agent，收集写操作证据。

        审计 R8：按 adapter_type 真分派 —— cli 走 subprocess；manual 不 spawn，
        抛 AgentUnavailableError 给可操作人工步骤；http 明确 NotImplementedError。"""
        if self.adapter_type == "manual":
            pfile = Path(cwd) / ".orch" / "agent_prompt.md"
            raise AgentUnavailableError(
                f"agent '{self.name}' 为 type=manual（无法自动执行，非 CLI 缺失）："
                f"请人工把提示词（将写入 {pfile}，或先以 --dry-run 产出 "
                f"specs/<task-id>/prompt.md）交给它的交互界面执行，"
                f"完成后 `orch run <task-id> --resume` 从 verify 续跑。"
            )
        if self.adapter_type == "http":
            raise NotImplementedError(
                f"agent '{self.name}' 声明 type=http，本批暂不支持 http 型 adapter"
                f"（未实现 API 直连骨架）；请改用 cli 型 agent 或等待实现。"
            )
        cmd = self.build_command(prompt, cwd, policy_hint)
        # 模板首 token 作 CLI 探测（shlex 已去引号；Windows 下 .cmd/.exe 由 which 自行补全）
        exe = shlex.split(self.command_template)[0]
        if shutil.which(exe) is None:
            raise AgentUnavailableError(
                f"未找到 agent '{self.name}' 的 CLI '{exe}'。{self.install_hint}"
            )

        before = self._git_name_status(cwd)
        try:
            # shell=True：Windows 上 npm 分发的 .cmd（codex/claude/aider 等）才能跨平台启动；
            # 列表先转平台适配的单命令串（Windows 用 list2cmdline，POSIX 用 shlex.join）。
            cmdline = (
                subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)
            )
            proc = subprocess.run(
                cmdline, cwd=str(cwd), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout, shell=True,
            )
            result = RunResult(
                exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentExecutionError(
                f"agent '{self.name}' 执行超时（{timeout}s），已终止：{exc}"
            ) from exc
        except OSError as exc:
            raise AgentExecutionError(f"agent '{self.name}' 启动失败: {exc}") from exc
        finally:
            # 无论成败都补收 diff（L3 即使事后发现也要补审，不能漏证据）
            after = self._git_name_status(cwd)

        # 写操作获取：优先 write_requests，其次 diff 推断
        if self.has("supports_write_requests"):
            result.write_requests = self.parse_write_requests(result.stdout)
        result.files_changed = self._diff_entries(before, after)
        return result

    # ---- 子类可覆写的解析钩子 --------------------------------------
    def parse_write_requests(self, stdout: str) -> list[dict[str, Any]]:
        """解析 agent 输出中的结构化写操作请求（默认无；有 write_requests 能力的子类覆写）。

        约定返回 [{"op": "write|delete|exec", "path": str, "detail": str}, ...]，由 policy 判定。
        """
        return []

    def parse_diff(self, stdout: str) -> list[str]:
        """returns_diff 的 agent：从 stdout 解析 diff 清单（可选增强，默认忽略）。"""
        return []

    # ---- git diff --name-status 推断 -------------------------------
    @staticmethod
    def _git_name_status(cwd: Path) -> set[str]:
        """快照当前改动：`git diff --name-status HEAD` + 未跟踪新文件（'?\t路径'）。"""
        entries: set[str] = set()
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-status", "HEAD"],
                cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            if proc.returncode == 0:
                entries |= {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
        except (OSError, subprocess.TimeoutExpired):
            return entries
        try:
            # 未跟踪文件不会出现在 diff 里，用 status --porcelain 补收；
            # -uall 展开未跟踪目录到文件级，避免 protected_paths 漏判（如 config/ 折叠）。
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            if proc.returncode == 0:
                for ln in proc.stdout.splitlines():
                    if ln.startswith("?? "):
                        entries.add(f"?\t{ln[3:].strip()}")
        except (OSError, subprocess.TimeoutExpired):
            pass
        return entries

    @staticmethod
    def _diff_entries(before: set[str], after: set[str]) -> list[str]:
        """执行前后取差集：agent 新产生的改动（执行前已存在的脏改动不计入本次）。

        过滤 .orch/ 前缀：那是编排器自己写的 prompt 文件，不是 agent 产出，
        否则会被误判 L2 污染 change-summary（审计 R2）。POSIX 分隔符归一后比对。
        """
        return sorted(e for e in (after - before)
                      if not e.split("\t")[-1].replace("\\", "/").startswith(".orch/"))
