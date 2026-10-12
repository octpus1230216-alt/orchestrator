"""orchestrator.core.policy
权限闸门：默认 auto，只拦 L3。

原则：
- 没被识别为 L3 的改动一律自动放行，不打扰用户；
- agent 只能"申请"写操作，编排器决定"放行/询问/拒绝"；
- 优先级：forbidden > require_approval(L3) > 默认 auto。

输入：写操作 WriteOp（类型 + 路径 + 原因 + 触及符号）
输出：AUTO | REQUIRE | FORBID

规则：
1 命中 forbidden            → FORBID
2 命中 require_approval(L3) → REQUIRE
3 其余                      → AUTO；属 L2 的记 logs/change-summary.md 摘要
4 路径升级：.github/、config.*、*.lock、Dockerfile → 强制 REQUIRE
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any

# 三档判定结果
AUTO = "AUTO"
REQUIRE = "REQUIRE"
FORBID = "FORBID"

# 严重度排序：聚合多个写操作时取最严的一档
_RANK = {AUTO: 0, REQUIRE: 1, FORBID: 2}

# config.policy 缺失时的兜底（与需求书一致）
DEFAULT_REQUIRE = [
    "modify_public_signature",
    "change_scope",
    "modify_schema",
    "add_dependency",
    "modify_ci_or_config",
    "delete_file",
    "modify_security_code",
    "merge_pr",
]
DEFAULT_FORBID = ["force_push", "push_main_directly"]

# L2：自动放行但记摘要（新增内部模块/工具函数、跨文件内部重构、新增测试）
L2_OPS = {"create_file", "add_module", "add_test", "refactor_internal", "rename_file"}

# 自由文本里出现即视为命中 forbidden（agent 用自然语言申请时兜底）
FORBID_ALIASES = {
    "force push": "force_push",
    "force-push": "force_push",
    "--force": "force_push",
    "push main directly": "push_main_directly",
    "push main": "push_main_directly",
    "push:main": "push_main_directly",
    "直推 main": "push_main_directly",
}

# 规则 4：路径升级 → 强制 REQUIRE（触发项按 modify_ci_or_config 计）
_ESCALATE_TRIGGER = "modify_ci_or_config"


@dataclass
class WriteOp:
    """一次写操作申请：类型 + 路径 + 原因 + 触及符号。"""

    op: str                       # 类型，如 modify_file / delete_file / modify_public_signature
    path: str = ""                # 目标文件（相对仓库根）
    reason: str = ""              # 申请原因
    symbol: str = ""              # 触及符号，如 "src/api/user.py::get_user(uid) -> User"
    raw: str = ""                 # 原始证据行（如 git name-status 'M\tpath'）

    @property
    def target(self) -> str:
        return self.symbol or self.path or self.op


@dataclass
class Judgement:
    write: WriteOp
    verdict: str                  # AUTO | REQUIRE | FORBID
    level: str                    # L1 | L2 | L3
    trigger: str = ""             # 命中的规则名（require/forbidden 键或路径升级）
    reason: str = ""


@dataclass
class PolicyResult:
    verdict: str                                   # 聚合结果（取最严）
    judgements: list[Judgement] = field(default_factory=list)

    @property
    def auto(self) -> list[WriteOp]:
        return [j.write for j in self.judgements if j.verdict == AUTO]

    @property
    def l2(self) -> list[Judgement]:
        """AUTO 且属 L2 的，需记 change-summary。"""
        return [j for j in self.judgements if j.verdict == AUTO and j.level == "L2"]

    @property
    def pending(self) -> list[Judgement]:
        return [j for j in self.judgements if j.verdict == REQUIRE]

    @property
    def forbidden(self) -> list[Judgement]:
        return [j for j in self.judgements if j.verdict == FORBID]


def _norm(op: str) -> str:
    return (op or "").strip().lower().replace("-", "_").replace(" ", "_")


def _escalate_by_path(path: str) -> bool:
    """规则 4：命中 CI/config/lock/Dockerfile 类路径 → 强制 REQUIRE。"""
    if not path:
        return False
    p = PurePosixPath(path.replace("\\", "/"))
    parts = [seg.lower() for seg in p.parts]
    name = parts[-1] if parts else ""
    if ".github" in parts:
        return True
    if "config" in parts[:-1]:            # config/ 目录下的任何文件
        return True
    for pat in ("config.*", "*.lock", "dockerfile*", "*.tf", "secrets*"):
        if fnmatch(name, pat):
            return True
    return False


def decide_write(w: WriteOp, config: dict[str, Any]) -> Judgement:
    """对单条写操作按规则 1→2→4→3 依次判定。"""
    pol = config.get("policy", {}) or {}
    forbidden = {_norm(x) for x in pol.get("forbidden", DEFAULT_FORBID)}
    require = {_norm(x) for x in pol.get("require_approval", DEFAULT_REQUIRE)}

    op = _norm(w.op)
    blob = f"{op} {_norm(w.path)} {w.reason.lower()}"

    # 规则 1：命中 forbidden → FORBID
    if op in forbidden:
        return Judgement(w, FORBID, "L3", trigger=op, reason="命中 forbidden")
    for alias, canon in FORBID_ALIASES.items():
        if alias in blob and canon in forbidden:
            return Judgement(w, FORBID, "L3", trigger=canon,
                             reason=f"命中 forbidden（文本匹配 '{alias}'）")

    # 规则 2：命中 require_approval(L3) → REQUIRE
    if op in require:
        return Judgement(w, REQUIRE, "L3", trigger=op, reason="命中 L3 require_approval")

    # 规则 4：路径升级 → 强制 REQUIRE
    if _escalate_by_path(w.path):
        return Judgement(w, REQUIRE, "L3", trigger=_ESCALATE_TRIGGER,
                         reason=f"路径升级强制审批: {w.path}")

    # 规则 3：其余 → AUTO（L2 记摘要，L1 完全不打扰）
    level = "L2" if op in L2_OPS else "L1"
    return Judgement(w, AUTO, level, reason="默认 auto 放行")


def decide(writes: list[WriteOp], config: dict[str, Any]) -> PolicyResult:
    """聚合判定：整体 verdict 取所有写操作里最严的一档。"""
    judgements = [decide_write(w, config) for w in writes]
    worst = AUTO
    for j in judgements:
        if _RANK[j.verdict] > _RANK[worst]:
            worst = j.verdict
    return PolicyResult(verdict=worst, judgements=judgements)
