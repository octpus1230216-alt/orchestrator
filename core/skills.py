"""orchestrator.core.skills
多 skill 能力库：skills/ 目录下多个 .md，每个内部分区：
    ## 约束 / ## 偏好 / ## 输出规范 / ## 反例

选用规则（config.skills）：
- explicit 非空 → 只用列出的
- 否则 auto_select: true → 按任务标签关键词自动选用（common 始终附带）
- 选用结果由调用方写入 state.json.skills_used；单一 skills.md（task 目录）仍兼容

注入规则：
- explore ← 「约束/偏好」→ context.md
- compile ← 「输出规范/反例」→ prompt.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: skill 内部建议分区
SECTIONS = ["约束", "偏好", "输出规范", "反例"]

#: 任务标签 → skill 名（关键词命中即选用；顺序即稳定输出顺序）
_TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "backend": ("接口", "服务", "后端", "api", "auth", "鉴权", "登录", "限流",
                "数据库", "route", "endpoint", "server", "session", "token"),
    "frontend": ("页面", "组件", "前端", "界面", "样式", "ui", "react", "vue",
                 "css", "button", "表单", "dashboard"),
    "data": ("数据", "报表", "分析", "指标", "etl", "管道", "pipeline",
             "sql", "图表", "数仓", "清洗", "dataset"),
}

#: 各阶段注入的分区
EXPLORE_SECTIONS = ("约束", "偏好")
COMPILE_SECTIONS = ("输出规范", "反例")


def skills_dir(config: dict[str, Any], root: Path) -> Path:
    d = config.get("skills", {}).get("dir", "skills/")
    p = Path(d)
    return p if p.is_absolute() else Path(root) / d


def list_skills(config: dict[str, Any], root: Path) -> list[str]:
    """目录下可用 skill 名（不含扩展名）。"""
    base = skills_dir(config, root)
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.md"))


def load_sections(path: Path) -> dict[str, str]:
    """把一个 skill 文件按 `## 分区` 切成 {分区名: 正文}。"""
    text = Path(path).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    current, buf = None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def select(requirement: str, config: dict[str, Any], root: Path) -> list[str]:
    """返回本任务选用的 skill 名列表（explicit 优先，否则按标签自动选用）。"""
    scfg = config.get("skills", {}) or {}
    available = set(list_skills(config, root))
    if scfg.get("explicit"):
        # 显式列出但目录里不存在的项不静默吞掉：保留在结果里，渲染时自然跳过
        return list(scfg["explicit"])
    if not scfg.get("auto_select", True):
        return []

    text = (requirement or "").lower()
    chosen: list[str] = []
    for tag, keywords in _TAG_KEYWORDS.items():
        if tag in available and any(kw.lower() in text for kw in keywords):
            chosen.append(tag)
    if "common" in available:
        chosen.append("common")       # 通用工程约束始终附带
    return chosen


def render(names: list[str], sections: tuple[str, ...], config: dict[str, Any],
           root: Path) -> str:
    """把若干 skill 的指定分区拼成一段注入文本（缺失 skill/分区跳过）。"""
    base = skills_dir(config, root)
    blocks: list[str] = []
    for name in names:
        path = base / f"{name}.md"
        if not path.exists():
            continue
        secs = load_sections(path)
        parts = [f"### {s}\n{secs[s]}" for s in sections if secs.get(s)]
        if parts:
            blocks.append(f"# skill: {name}\n" + "\n".join(parts))
    return "\n\n".join(blocks)


def for_explore(names: list[str], config: dict[str, Any], root: Path) -> str:
    return render(names, EXPLORE_SECTIONS, config, root)


def for_compile(names: list[str], config: dict[str, Any], root: Path) -> str:
    return render(names, COMPILE_SECTIONS, config, root)
