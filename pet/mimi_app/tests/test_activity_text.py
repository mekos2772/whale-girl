"""Presentation-layer tests for the head-top activity capsule.

Locking the semantic-compression behaviour the user asked for: never a naive
character truncation of a long sentence, always a short "动作 + 对象" line in
6-14 Chinese chars, and a sensible harness display name.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.activity_text import (  # noqa: E402
    compress_summary_to,
    formatHarnessActivity,
    getCompactActivityText,
    getHarnessDisplayName,
    tool_row_text,
    tool_summary,
)


class HarnessDisplayNameTests(unittest.TestCase):
    def test_known_mappings(self) -> None:
        cases = {
            "Claude Code": "Claude",
            "OpenAI Codex CLI": "Codex",
            "Codex CLI": "Codex",
            "DeepSeek Harness": "DSH",
            "DeepSeek Shell Harness": "DSH",
            "Gemini CLI": "Gemini",
            "Cursor Agent": "Cursor",
        }
        for raw, expected in cases.items():
            self.assertEqual(getHarnessDisplayName(raw), expected, raw)

    def test_generic_agent_presets_yield_no_badge(self) -> None:
        for raw in ("standard", "vision", "web", "desktop", "", "default"):
            self.assertEqual(getHarnessDisplayName(raw), "", raw)

    def test_unknown_name_strips_empty_suffixes(self) -> None:
        # "CLI" here is mid-string (not a suffix) so it stays; only trailing
        # empty suffixes are stripped.
        self.assertEqual(getHarnessDisplayName("MyCompany CLI Pro"), "Mycompany Cli Pro")
        self.assertEqual(getHarnessDisplayName("Some Adapter"), "Some")
        self.assertEqual(getHarnessDisplayName("  "), "")


class CompactActivityTextTests(unittest.TestCase):
    def test_long_sentence_is_semantically_compressed_not_char_cut(self) -> None:
        text = "正在分析用户上传的第三张图片，并根据第二张图片调整左右手脚位置"
        result = getCompactActivityText(text)
        self.assertLessEqual(len(result), 16)
        self.assertTrue(result.startswith("调整"))
        self.assertTrue(result.endswith("…"))
        self.assertNotIn("用户在第三张图片", result)  # not a raw prefix cut
        self.assertNotIn("正确的位置，并根据", result)

    def test_search_pr_compresses_to_check_brand(self) -> None:
        text = "正在调用 GitHub 搜索 DeepGEMM 最近的 Pull Request"
        result = getCompactActivityText(text)
        self.assertEqual(result, "检查 DeepGEMM PR…")

    def test_waiting_confirmation_compresses(self) -> None:
        self.assertEqual(
            getCompactActivityText("Waiting for user confirmation before applying changes"),
            "等待你确认…",
        )
        self.assertEqual(getCompactActivityText("正在等待你确认调整方案"), "等待你确认…")

    def test_title_keeps_verb_and_object(self) -> None:
        self.assertEqual(getCompactActivityText("正在构建一个 Windows 桌面宠物"), "构建 Windows 桌面宠物…")
        self.assertEqual(getCompactActivityText("检查 DSH 结构"), "检查 DSH 结构…")

    def test_english_verb_led(self) -> None:
        self.assertEqual(getCompactActivityText("Checking the project structure"), "检查 project…")

    def test_tight_variant_stays_boundary_aware(self) -> None:
        tight = compress_summary_to("检查 DSH 结构…", 6)
        self.assertTrue(tight.endswith("…"))
        self.assertLessEqual(len(tight), 8)


class ToolLineTests(unittest.TestCase):
    def test_row_text_uses_official_titles_and_minimal_detail(self) -> None:
        self.assertEqual(
            tool_row_text("pwsh", '{"command":"git status --short; python -m pytest"}'),
            "Pwsh · python -m pytest",
        )
        self.assertEqual(
            tool_row_text("read", '{"path":"C:/x/model.json"}'),
            "Read · model.json",
        )

    def test_summary_verb_object(self) -> None:
        self.assertEqual(
            tool_summary("edit", '{"file_path":"mimi_app/config/app.json"}'),
            "修改 app.json…",
        )
        self.assertEqual(
            tool_summary("web_search", '{"query":"DeepSeek 更新"}'),
            "搜索 DeepSeek 更新…",
        )
        self.assertEqual(
            tool_summary("pwsh", '{"command":"python -m unittest"}'),
            "运行 python -m unittest…",
        )


class FormatTests(unittest.TestCase):
    def test_format_harness_activity(self) -> None:
        self.assertEqual(formatHarnessActivity("DSH", "检查 DSH 结构…"), "DSH · 检查 DSH 结构…")
        self.assertEqual(formatHarnessActivity("", "检查 DSH 结构…"), "检查 DSH 结构…")
        self.assertEqual(formatHarnessActivity("Codex", ""), "Codex")


if __name__ == "__main__":
    unittest.main()
