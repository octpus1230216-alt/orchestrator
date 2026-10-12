"""单测 2：prompt 分段（parse_segments / build_segment_prompt / resolve_segment_mode）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.compile import parse_segments  # noqa: E402
from core.dispatch import (  # noqa: E402
    SEG_PER,
    SEG_WHOLE,
    build_segment_prompt,
    resolve_segment_mode,
)

_PROMPT = """\
# 总任务
前言若干行。

## SEGMENT 1
第一段正文 A1
第二段不该出现的内容占位

## SEGMENT 2
第二段正文 B1
"""


class TestParseSegments(unittest.TestCase):

    def test_two_segments(self):
        segs = parse_segments(_PROMPT)
        self.assertEqual([s["index"] for s in segs], [1, 2])
        self.assertIn("第一段正文 A1", segs[0]["body"])
        self.assertNotIn("## SEGMENT", segs[0]["body"])  # 标题不进正文
        self.assertIn("第二段正文 B1", segs[1]["body"])
        self.assertNotIn("第一段正文 A1", segs[1]["body"])

    def test_no_marker_single_whole(self):
        segs = parse_segments("# 只有正文\n没有分段标题")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["title"], "(whole)")
        self.assertEqual(segs[0]["index"], 1)


class TestBuildSegmentPrompt(unittest.TestCase):

    def test_context_injection_without_prev_prompt(self):
        """段间上下文：只注入摘要+文件清单+context 全文，不重塞前序段整份 prompt。"""
        out = build_segment_prompt(
            "SEGMENT 2 正文", context_md="CTX 全文",
            prev_files=["src/a.py"], prev_summaries=["SEGMENT 1: 做了 a"])
        self.assertIn("SEGMENT 2 正文", out)
        self.assertIn("## 已完成进度", out)
        self.assertIn("src/a.py", out)
        self.assertIn("SEGMENT 1: 做了 a", out)
        self.assertIn("CTX 全文", out)
        self.assertNotIn("CTX 全文CTX 全文", out)

    def test_first_segment_bare(self):
        """首段无历史：不带已完成进度块。"""
        out = build_segment_prompt("正文", "", [], [])
        self.assertNotIn("已完成进度", out)


class TestResolveSegmentMode(unittest.TestCase):

    CFG = {"segment_mode": "auto", "segment_threshold": 100}

    def test_auto_below_threshold_is_whole(self):
        self.assertEqual(resolve_segment_mode(50, self.CFG), SEG_WHOLE)

    def test_auto_above_threshold_is_per_segment(self):
        self.assertEqual(resolve_segment_mode(150, self.CFG), SEG_PER)

    def test_override_wins(self):
        self.assertEqual(resolve_segment_mode(50, self.CFG, override="per_segment"), SEG_PER)
        self.assertEqual(resolve_segment_mode(99999, self.CFG, override="whole"), SEG_WHOLE)


if __name__ == "__main__":
    unittest.main()
