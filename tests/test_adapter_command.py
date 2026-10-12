"""单测 1：adapter 变量替换（{prompt_file} / {prompt_inline} / 未配置模板）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.base import AgentAdapter, AgentUnavailableError  # noqa: E402


class TestAdapterVariableSubstitution(unittest.TestCase):

    def test_prompt_file_token(self):
        """{prompt_file}：替换为 worktree 内 .orch/agent_prompt.md，且文件已写入 prompt+policy_hint。"""
        ad = AgentAdapter({"command_template": "codex exec --prompt-file {prompt_file}"})
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            argv = ad.build_command("做点什么", cwd, policy_hint="HINT")
            self.assertEqual(argv[:3], ["codex", "exec", "--prompt-file"])
            pfile = Path(argv[3])
            self.assertEqual(pfile, cwd / ".orch" / "agent_prompt.md")
            content = pfile.read_text(encoding="utf-8")
            self.assertIn("做点什么", content)
            self.assertIn("HINT", content)

    def test_prompt_inline_token(self):
        """{prompt_inline}：整份 prompt 作为单个 argv token 传入。"""
        ad = AgentAdapter({"command_template": "claude -p {prompt_inline}"})
        with tempfile.TemporaryDirectory() as tmp:
            argv = ad.build_command("内联提示词", Path(tmp))
            self.assertEqual(argv[0], "claude")
            self.assertEqual(argv[2], "内联提示词")

    def test_no_placeholder(self):
        """模板不含占位符：原样拆分为 argv。"""
        ad = AgentAdapter({"command_template": "myagent --auto"})
        with tempfile.TemporaryDirectory() as tmp:
            argv = ad.build_command("x", Path(tmp))
            self.assertEqual(argv, ["myagent", "--auto"])

    def test_missing_template_raises(self):
        """未配置 command_template → AgentUnavailableError 附安装提示。"""
        ad = AgentAdapter({"command_template": "", "install_hint": "装好再来"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentUnavailableError) as ctx:
                ad.build_command("x", Path(tmp))
            self.assertIn("装好再来", str(ctx.exception))

    def test_capabilities_lookup(self):
        ad = AgentAdapter({
            "command_template": "a {prompt_file}",
            "capabilities": {"supports_prompt_file": True, "returns_diff": True},
        })
        self.assertTrue(ad.has("supports_prompt_file"))
        self.assertTrue(ad.has("returns_diff"))
        self.assertFalse(ad.has("supports_streaming"))
        ad.requires(supports_prompt_file=True)  # 满足则不抛
        with self.assertRaises(AgentUnavailableError):
            ad.requires(supports_resume=True)   # 不满足则抛


if __name__ == "__main__":
    unittest.main()
