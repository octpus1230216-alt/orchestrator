"""orchestrator.core.fingerprint
任务指纹与 task-id 生成（doc_id 支持 file_bytes ↔ content 双模式，调用方不改）。

## 背景（为什么 doc_id 在"重新导出"后会变）
python-docx / openpyxl / python-pptx 保存时会把 zip 时间戳写进包内
（zip 每个 entry 的 date_time，以及 docProps/core.xml 的 created/modified），
同一内容两次导出得到的字节必然不同 → doc_id=sha256(file_bytes) 随之变化 →
被判为"新文档"。
因此：
- 「同一份文件重跑幂等」成立（文件没动，指纹自然不变）；
- 「重新导出的文件」会被当新文档（自检脚本每轮清目录重造即源于此）。

## 当前决策
- 生产语义保持 mode: file_bytes（需求明确 sha256(file_bytes)）；
- 本模块仅显式记录当前用字节指纹，并在 config.fingerprint 预留切换点；
- 文档写清副作用，避免后人误以为 doc_id 在跨导出场景下稳定。

## 切换路径
- 需求变为"跨导出保持 doc_id 稳定"时：config.fingerprint.mode 改 content；
- content 模式启用 normalize（忽略 zip 时间戳/docProps 等易变字段），
  调用方 compute_doc_id(path) / doc_id(task_dir) 的用法完全不改；
- 对"未重新导出"的普通文本文件（md/json/txt/py…无可归一化的易变字段），
  content 模式直接回退为原始字节哈希 → 与 file_bytes 产生一致的值，
  切换模式不会让这类文档一次性变成"新文档"；
  而对 zip 容器（docx/xlsx/pptx/whl…）两种模式值必然不同——若历史上
  已用 file_bytes 落过 doc_id，切换后首轮会像"新文档"一次（一次性迁移），
  之后跨导出保持稳定。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

DEFAULT_ALGORITHM = "sha256"

#: content 模式默认归一化链（可被 config.fingerprint.content.normalize 覆盖）
DEFAULT_NORMALIZE = ["strip_zip_timestamps", "strip_docprops", "sort_xml_attrs",
                     "canonical_json", "drop_rels_timestamps"]


def make_task_id(requirement: str, max_slug: int = 24) -> str:
    """由需求文本生成 `<slug>-<8位hash>` 形式的 task-id。"""
    slug = re.sub(r"[^a-z0-9]+", "-", requirement.lower()).strip("-")
    slug = slug[:max_slug].strip("-") or "task"
    digest = hashlib.sha1(requirement.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


# ---------------------------------------------------------------- 模式解析
def _mode_algo(config: dict[str, Any] | None) -> tuple[str, str]:
    """从 config 取 (mode, algorithm)；兼容旧 runtime.fingerprint_mode。"""
    fp = (config or {}).get("fingerprint", {}) or {}
    mode = fp.get("mode") or (config or {}).get("runtime", {}).get(
        "fingerprint_mode", "file_bytes")
    algo = fp.get("algorithm") or DEFAULT_ALGORITHM
    return mode, algo


def _digest(algo: str, data: bytes) -> str:
    return f"{algo}:{getattr(hashlib, algo)(data).hexdigest()}"


# ---------------------------------------------------------------- 提取与归一化
def _is_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _extract_content(data: bytes) -> tuple[bool, list[tuple[str, bytes]]]:
    """把文件拆成可归一化的 (name, bytes) 列表；非 zip 返回 (False, [(单条)])。"""
    if _is_zip(data):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = [(zi.filename.replace("\\", "/"), zf.read(zi.filename))
                       for zi in zf.infolist()]
            return True, entries
    return False, [("", data)]


_XML_TAG_RE = re.compile(rb"<([A-Za-z_][\w.:-]*)((?:\s+[^<>]*?)?)(/?)>")
_XML_ATTR_RE = re.compile(rb'([\w.:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def _sort_xml_attrs(data: bytes) -> bytes:
    """标签内属性按名称排序后重拼（不解析整个 XML，足以消除属性顺序抖动）。"""
    def repl(m: re.Match) -> bytes:
        tag, attrs_blob, selfclose = m.group(1), m.group(2), m.group(3)
        attrs = [(k, v) for k, _, v, _ in _XML_ATTR_RE.findall(attrs_blob)]
        if not attrs:
            return m.group(0)
        joined = b" " + b" ".join(k + b'="' + v + b'"' for k, v in sorted(attrs))
        return b"<" + tag + joined + (b" " if selfclose else b"") + selfclose + b">"
    return _XML_TAG_RE.sub(repl, data)


#: 归一化器：入参 (is_zip, entries)，出参 entries；对应 config normalize 名单
_NORMALIZERS: dict[str, Callable[[bool, list[tuple[str, bytes]]],
                                 list[tuple[str, bytes]]]] = {
    # zip 每个条目的 date_time 在提取阶段就没参与哈希 → 此处即"忽略时间戳"声明
    "strip_zip_timestamps": lambda is_zip, es: es,
    # docx/pptx 的 created/modified 藏在 docProps/core.xml，直接剔除整棵目录
    "strip_docprops": lambda is_zip, es: [e for e in es
                                          if not e[0].startswith("docProps/")],
    "sort_xml_attrs": lambda is_zip, es: [
        (n, _sort_xml_attrs(b) if n.endswith((".xml", ".rels")) else b)
        for n, b in es],
    "canonical_json": lambda is_zip, es: [
        (n, json.dumps(json.loads(b.decode("utf-8")), sort_keys=True,
                       ensure_ascii=False).encode("utf-8"))
        if n.endswith(".json") else (n, b)
        for n, b in es],
    # .rels 条目的 zip 时间戳同样不采（content 模式提取时已忽略 date_time），
    # 独立开关用于表达"关系文件时间戳必须被丢弃"这一显式意图
    "drop_rels_timestamps": lambda is_zip, es: es,
}


def _canonical_bytes(path: Path, normalize: list[str]) -> bytes:
    """content 模式：提取内容并按归一化链处理，返回规范字节。

    非 zip 文件且归一化后字节无变化 → 直接返回原始字节，
    使其与 file_bytes 模式产生一致哈希（满足"切换模式调用方不改、
    未重新导出的普通文本文件指纹不变"）。
    """
    data = path.read_bytes()
    is_zip, entries = _extract_content(data)
    if not is_zip and not path.name.endswith(".json"):
        return data                       # 普通文本/二进制：与 file_bytes 同值
    for name in normalize:
        fn = _NORMALIZERS.get(name)
        if fn:
            entries = fn(is_zip, entries)   # 归一化器只变换 entries
    h = hashlib.sha256()
    for name, blob in sorted(entries, key=lambda e: e[0]):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(blob).digest())
    return h.digest()                     # 规范摘要当"内容字节"再走外层哈希


# ---------------------------------------------------------------- 对外接口
def compute_doc_id(path: Any, config: dict[str, Any] | None = None) -> str:
    """单文件文档指纹：`<algorithm>:<hexdigest>`。

    mode == "file_bytes": sha256(read_bytes(path))
    mode == "content":    sha256(normalize(extract_content(path)))
    调用方不感知模式，只改 config.fingerprint.mode 即可切换。
    """
    mode, algo = _mode_algo(config)
    path = Path(path)
    if mode == "content":
        normalize = ((config or {}).get("fingerprint", {}).get("content", {})
                     or {}).get("normalize", DEFAULT_NORMALIZE)
        return _digest(algo, _canonical_bytes(path, normalize))
    return _digest(algo, path.read_bytes())


#: 参与任务指纹的关键规格文档（存在才计入）
_DOC_FILES = ["context.md", "skills.md", "prompt.md", "acceptance.md"]


def doc_id(task_dir: Path, config: dict[str, Any] | None = None,
           mode: str | None = None) -> str:
    """汇总 specs/<task-id>/ 关键规格文档计算 doc_id（state.json 契约字段）。

    模式取 config.fingerprint.mode（或显式 mode 参数），算法取 config。
    content 模式下每个文件先 compute_doc_id 归一化，再串接聚合。
    """
    eff_mode, algo = _mode_algo(config)
    mode = mode or eff_mode
    eff_cfg = dict(config or {})
    if mode != eff_mode:
        eff_cfg.setdefault("fingerprint", {})["mode"] = mode

    h = getattr(hashlib, algo)()
    for name in _DOC_FILES:
        f = Path(task_dir) / name
        if not f.exists():
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        if mode == "content":
            normalize = (eff_cfg.get("fingerprint", {}).get("content", {}) or {}).get(
                "normalize", DEFAULT_NORMALIZE)
            h.update(_canonical_bytes(f, normalize))
        else:
            h.update(f.read_bytes())
        h.update(b"\0")
    return f"{algo}:{h.hexdigest()}"


def content_fingerprint(path: Path) -> str:
    """（兼容旧接口）单文件原始字节指纹，等价 compute_doc_id(path, file_bytes)。"""
    return compute_doc_id(path, {"fingerprint": {"mode": "file_bytes"}})
