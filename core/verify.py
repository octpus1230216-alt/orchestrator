"""orchestrator.core.verify
阶段 5：验收。

逐条读取 acceptance.md 中的**可执行命令**，在隔离工作区（worktree）内执行；
每条命令支持行尾注释选项：`# timeout=30 optional 任意备注`
  - timeout=N     单条命令超时秒数（缺省取 config.timeouts.verify_cmd）
  - optional      失败不阻断验收（默认每条是 required）
setup/teardown 段用于起停服务（失败不计入 passed）；输出过长截断（保头尾）。
结果写 result/verify_report.json；任一 required 命令失败 → passed=False → heal。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
_OPTS_RE = re.compile(r"\b(timeout|optional|required)\b(?:=(\d+|[a-zA-Z]+))?")


def _parse_line(line: str, default_timeout: int) -> dict[str, Any] | None:
    """一行命令 → {command, timeout, required, note}；行尾 `# ...` 解析选项。

    仅在 # 前是空白时视为注释起点，避免误伤命令内的 #。
    """
    command = re.split(r"\s#", line, maxsplit=1)[0].strip()
    if not command:
        return None
    item = {"command": command, "timeout": default_timeout, "required": True,
            "note": ""}
    comment = ""
    if "#" in line:
        comment = line.split("#", 1)[1]
    for m in _OPTS_RE.finditer(comment):
        key = m.group(1)
        if key == "timeout" and m.group(2):
            item["timeout"] = int(m.group(2))
        elif key == "optional":
            # 裸 `optional` → 非必需；`optional=false` → 仍必需
            item["required"] = (m.group(2) or "").lower() in ("false", "no")
        elif key == "required" and m.group(2):
            item["required"] = m.group(2).lower() in ("true", "yes")
    # 注释去掉选项 token 后剩下的自然语言当备注
    item["note"] = _OPTS_RE.sub("", comment).strip(" =,")
    return item


def parse_acceptance(md: str, default_timeout: int = 600) -> dict[str, list[Any]]:
    """把 acceptance.md 解析为 {setup: [...], check: [...], teardown: [...]}。

    - 代码围栏 ``` 内的行按所在小标题归类：标题含 setup/teardown 归对应段，否则为 check。
    - 以 # 开头的行视为注释忽略；行尾 # 注释剥离并提取 timeout/optional 选项。
    - setup/teardown 返回命令字符串列表（不参与 passed）；check 返回选项 dict 列表。
    """
    sections: dict[str, list[Any]] = {"setup": [], "check": [], "teardown": []}
    current = "check"
    in_fence = False
    for raw in md.splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        h = _HEADING_RE.match(raw)
        if h and not in_fence:
            title = h.group(1).lower()
            if "setup" in title:
                current = "setup"
            elif "teardown" in title:
                current = "teardown"
            elif "check" in title or "验收" in title or "检查" in title:
                current = "check"
            continue
        if not in_fence:
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if current == "check":
            item = _parse_line(line, default_timeout)
            if item:
                sections["check"].append(item)
        else:
            cmd = re.split(r"\s#", line, maxsplit=1)[0].strip()
            if cmd:
                sections[current].append(cmd)
    return sections


def _truncate(text: str, keep_head: int, keep_tail: int) -> str:
    """输出过长截断：保留头 keep_head 行与尾 keep_tail 行。"""
    lines = text.splitlines()
    if len(lines) <= keep_head + keep_tail:
        return text
    omitted = len(lines) - keep_head - keep_tail
    head = "\n".join(lines[:keep_head])
    tail = "\n".join(lines[-keep_tail:]) if keep_tail else ""
    return f"{head}\n... [截断 {omitted} 行] ...\n{tail}"


def _exec(cmd: str, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return {
            "command": cmd,
            "returncode": proc.returncode,
            "timed_out": False,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"command": cmd, "returncode": None, "timed_out": True, "stdout": "", "stderr": "timeout"}


def run(
    task_dir: Path,
    config: dict[str, Any],
    *,
    worktree: Path | None = None,
) -> dict[str, Any]:
    """在 worktree 内逐条执行 acceptance.md 命令，写 result/verify_report.json。

    passed 语义：任一 **required** 命令失败即 False；optional 失败仅记录不阻断。
    """
    task_dir = Path(task_dir)
    worktree = Path(worktree) if worktree else task_dir
    vcfg = config.get("verify", {})
    # 命令级 timeout 缺省值：优先 config.timeouts.verify_cmd
    default_timeout = config.get("timeouts", {}).get(
        "verify_cmd", vcfg.get("default_timeout_seconds", 600)
    )
    keep_head = vcfg.get("keep_head_lines", 40)
    keep_tail = vcfg.get("keep_tail_lines", 40)

    acc = task_dir / "acceptance.md"
    if not acc.exists():
        report = {"passed": False, "error": "acceptance.md 不存在", "results": []}
        _write(task_dir, report)
        return report

    sections = parse_acceptance(acc.read_text(encoding="utf-8"), int(default_timeout))

    for s in sections["setup"]:
        _exec(s, worktree, int(default_timeout))

    results: list[dict[str, Any]] = []
    try:
        for item in sections["check"]:
            r = _exec(item["command"], worktree, int(item["timeout"]))
            r["passed"] = (r["returncode"] == 0)
            r["required"] = item["required"]
            if item.get("note"):
                r["note"] = item["note"]
            r["stdout"] = _truncate(r["stdout"], keep_head, keep_tail)
            r["stderr"] = _truncate(r["stderr"], keep_head, keep_tail)
            results.append(r)
    finally:
        for s in sections["teardown"]:
            _exec(s, worktree, int(default_timeout))

    required_failed = [r for r in results if r["required"] and not r["passed"]]
    report = {
        "passed": bool(results) and not required_failed,
        "total": len(results),
        "failed": sum(1 for r in results if not r["passed"]),
        "required_failed": [r["command"] for r in required_failed],
        "results": results,
    }
    _write(task_dir, report)
    return report


def _write(task_dir: Path, report: dict[str, Any]) -> Path:
    out_dir = task_dir / "result"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "verify_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
