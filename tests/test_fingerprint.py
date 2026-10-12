"""单测 6（附加）：指纹切换 file_bytes ↔ content 调用方不改。

覆盖两条需求：
- 未重新导出的普通文本文件：两种模式产生一致 doc_id（切换不是迁移）；
- 重新导出的 zip（同内容、zip 时间戳不同）：file_bytes 判为新文档，
  content 判为同一文档（跨导出稳定）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fingerprint import compute_doc_id, doc_id  # noqa: E402

CFG_BYTES = {"fingerprint": {"mode": "file_bytes", "algorithm": "sha256"}}
CFG_CONTENT = {"fingerprint": {"mode": "content", "algorithm": "sha256",
                               "content": {"normalize": [
                                   "strip_zip_timestamps", "strip_docprops",
                                   "sort_xml_attrs", "canonical_json",
                                   "drop_rels_timestamps"]}}}


def _make_zip(path: Path, date_time: tuple) -> None:
    """写一个 docx 风格 zip：内容固定，仅 zip entry 时间戳不同。"""
    with zipfile.ZipFile(path, "w") as zf:
        zi = zipfile.ZipInfo("word/document.xml", date_time=date_time)
        zf.writestr(zi, "<w:doc><w:p>同一内容</w:p></w:doc>")
        zi2 = zipfile.ZipInfo("docProps/core.xml", date_time=date_time)
        zf.writestr(zi2, "<cp:core created='2020-01-01T00:00:00Z'/>")


class TestComputeDocId(unittest.TestCase):

    def test_plain_text_stable_across_modes(self):
        """普通文本文件：content 与 file_bytes 值一致 → 调用方切换模式不改。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "context.md"
            f.write_text("# 同一份文件\n重跑幂等\n", encoding="utf-8")
            self.assertEqual(compute_doc_id(f, CFG_BYTES),
                             compute_doc_id(f, CFG_CONTENT))

    def test_file_bytes_idempotent_rerun(self):
        """同一份文件重跑幂等（file_bytes 模式本意）。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.txt"
            f.write_text("x", encoding="utf-8")
            self.assertEqual(compute_doc_id(f, CFG_BYTES), compute_doc_id(f, CFG_BYTES))

    def test_reexported_zip(self):
        """重新导出（仅 zip 时间戳变）：file_bytes 判新文档；content 判同一文档。"""
        with tempfile.TemporaryDirectory() as tmp:
            v1 = Path(tmp) / "doc1.docx"
            v2 = Path(tmp) / "doc2.docx"
            _make_zip(v1, (2020, 1, 1, 0, 0, 0))
            _make_zip(v2, (2023, 6, 6, 12, 0, 0))
            # 字节不同（时间戳写进包内）
            self.assertNotEqual(v1.read_bytes(), v2.read_bytes())
            self.assertNotEqual(compute_doc_id(v1, CFG_BYTES),
                                compute_doc_id(v2, CFG_BYTES),
                                "file_bytes 把重新导出判为新文档（已知副作用）")
            self.assertEqual(compute_doc_id(v1, CFG_CONTENT),
                             compute_doc_id(v2, CFG_CONTENT),
                             "content 模式跨导出保持稳定")


class TestDocIdAggregation(unittest.TestCase):

    def test_task_dir_same_api_both_modes(self):
        """doc_id(task_dir) 接口不变：md 规格目录两种模式都可调用且值一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / "context.md").write_text("ctx", encoding="utf-8")
            (task / "prompt.md").write_text("prompt", encoding="utf-8")
            (task / "acceptance.md").write_text("acc", encoding="utf-8")
            a = doc_id(task, CFG_BYTES)
            b = doc_id(task, CFG_CONTENT)
            self.assertTrue(a and a == b)


if __name__ == "__main__":
    unittest.main()
