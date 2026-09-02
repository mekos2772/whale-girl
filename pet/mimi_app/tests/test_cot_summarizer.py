"""Reply summarizer tests: local heuristics, DSH config resolution, and the
integration flow (assistant replies get summaries; reasoning NEVER does).

All offline: the LLM call is faked on the worker thread path.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

import mimi_pet.cot_summarizer as cot  # noqa: E402
from mimi_pet.dsh_bridge import DshEvent  # noqa: E402
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.action_library import ActionLibrary  # noqa: E402


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


class LocalSummaryTests(unittest.TestCase):
    def test_short_text_passes_through(self) -> None:
        text = "先检查配置，再启动服务。"
        self.assertEqual(cot.local_summary(text), text)

    def test_long_text_prefers_conclusion_markers(self) -> None:
        text = (
            "首先我们要检查网络连接是否正常，然后读取配置文件，"
            "接着尝试多次重试避免瞬时错误，最后综合以上分析，结论是使用缓存方案最合适。"
            "后续还可以考虑优化查询性能并添加更多测试覆盖各种边界情况。"
        )
        summary = cot.local_summary(text, max_chars=30)
        self.assertIn("结论", summary)
        self.assertLessEqual(len(summary), 31)

    def test_whitespace_collapsed(self) -> None:
        self.assertEqual(cot.local_summary("  a\n  b  c "), "a b c")


class ConfigResolutionTests(unittest.TestCase):
    def test_builtin_provider_spec(self) -> None:
        spec = cot.resolve_provider_spec("opencode-go")
        self.assertIn("base", spec)
        self.assertIn("opencode", spec["base"])
        self.assertEqual(spec["protocol"], "openai-completions")
        self.assertIn("deepseek-v4-flash", spec["models"])

    def test_deepseek_official_builtin_resolves_auto_default(self) -> None:
        """DSH's agent-default-model uses the first-party deepseek-official
        provider, which lives in NO pi-ai catalog file — the builtin registry
        must carry its endpoint and DEEPSEEK_API_KEY credential name."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "settings.yaml").write_text(
                "agent-default-model:\n"
                "  provider: deepseek-official\n"
                "  model: deepseek-v4-flash-vision-exp\n",
                encoding="utf-8",
            )
            (home / ".credentials.yaml").write_text(
                "DEEPSEEK_API_KEY: sk-ds-test\n", encoding="utf-8"
            )
            summarizer = cot.CoTSummarizer(
                settings_path=home / "settings.yaml",
                credentials_path=home / ".credentials.yaml",
            )
            spec = summarizer._resolve_spec()
            self.assertEqual(spec["provider"], "deepseek-official")
            self.assertEqual(spec["model"], "deepseek-v4-flash-vision-exp")
            self.assertEqual(spec["base"], "https://api.deepseek.com")
            self.assertEqual(spec["protocol"], "openai-completions")
            self.assertEqual(spec["api_key"], "sk-ds-test")
            self.assertIsNone(summarizer.last_error)

    def test_load_api_keys_accepts_refs_and_flat(self) -> None:
        """DSH's newer credential format nests pairs under ``refs``."""
        self.assertEqual(cot.load_api_keys({"refs": {"A_KEY": "x", "B": 1}}), {"A_KEY": "x", "B": "1"})
        self.assertEqual(cot.load_api_keys({"A_KEY": "x"}), {"A_KEY": "x"})

    def test_fake_dsh_home_resolves_agent_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "settings.yaml").write_text(
                "agent-default-model:\n"
                "  provider: lordfa\n"
                "  model: glm-5.2\n"
                "llm-pi-ai:\n"
                "  providers:\n"
                "    lordfa:\n"
                "      apiKeyEnv: LORDFA_API_KEY\n"
                "      api: openai-responses\n"
                "      baseURL: https://api.lordfa.top/v1\n",
                encoding="utf-8",
            )
            (home / ".credentials.yaml").write_text(
                "LORDFA_API_KEY: sk-test-123\n", encoding="utf-8"
            )
            summarizer = cot.CoTSummarizer(
                settings_path=home / "settings.yaml",
                credentials_path=home / ".credentials.yaml",
            )
            spec = summarizer._resolve_spec()
            self.assertEqual(spec["provider"], "lordfa")
            self.assertEqual(spec["model"], "glm-5.2")
            self.assertEqual(spec["base"], "https://api.lordfa.top/v1")
            self.assertEqual(spec["protocol"], "openai-responses")
            self.assertEqual(spec["api_key"], "sk-test-123")

    def test_summarize_uses_llm_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "settings.yaml").write_text(
                "agent-default-model:\n  provider: opencode-go\n  model: deepseek-v4-flash\n",
                encoding="utf-8",
            )
            (home / ".credentials.yaml").write_text(
                "OPENCODE_GO_API_KEY: sk-test-123\n", encoding="utf-8"
            )
            summarizer = cot.CoTSummarizer(
                settings_path=home / "settings.yaml",
                credentials_path=home / ".credentials.yaml",
            )
            original = cot._http_json

            def fake_http(url, body, api_key, timeout=15.0):
                self.assertIn("chat/completions", url)
                self.assertEqual(api_key, "sk-test-123")
                return {"choices": [{"message": {"content": "结论：使用缓存。"}}]}

            cot._http_json = fake_http
            try:
                self.assertEqual(summarizer.summarize("很长的回复内容" * 50), "结论：使用缓存。")
            finally:
                cot._http_json = original

            # Network failure -> local heuristic, no exception.
            def failing_http(url, body, api_key, timeout=15.0):
                raise OSError("offline")

            cot._http_json = failing_http
            try:
                summary = summarizer.summarize("回复 " * 100)
                self.assertTrue(summary)
                self.assertIsNotNone(summarizer.last_error)
            finally:
                cot._http_json = original


class FakeCot:
    enabled = True
    provider = "auto"
    model = None
    last_error = None

    def __init__(self, result: str) -> None:
        self.result = result

    def summarize(self, text: str) -> str:
        return self.result

    def configure(self, provider, model=None) -> None:
        self.provider, self.model = provider, model

    def model_choices(self):
        return [("自动（跟随 DSH）", "auto", "")]


LONG_REPLY = "已经帮你查好了：现在是晚上十一点半，电量还剩百分之八十七，网速下载每秒大概十二兆。" * 2


class ReplySummaryFlowTests(unittest.TestCase):
    """The summary feature targets the assistant's NORMAL output only."""

    @staticmethod
    def _chunk(turn, step, chunk):
        return DshEvent(
            method="session/event",
            rpc_id="x",
            payload={
                "sessionId": "s",
                "event": {
                    "type": "assistant/chunk",
                    "data": {"turn": turn, "step": step, "chunk": chunk},
                },
            },
        )

    @staticmethod
    def _session_event(event_type, data):
        return DshEvent(
            method="session/event",
            rpc_id="x",
            payload={"sessionId": "s", "event": {"type": event_type, "data": data}},
        )

    def _integration(self, result: str):
        integration = DshIntegration(make_engine())
        integration.cot = FakeCot(result)

        class Sink:
            def __init__(self) -> None:
                self.bubbles: list[tuple[str, str]] = []

            def show_bubble(self, text, kind="assistant"):
                self.bubbles.append((kind, text))

            def set_row(self, *a):
                pass

        sink = Sink()
        integration.sink = sink
        return integration, sink

    def test_long_reply_gets_a_summary_bubble(self) -> None:
        integration, sink = self._integration("已压缩的回复要点")
        integration._last_bubble_at = -1e9
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": LONG_REPLY}))
        integration._handle_event(self._session_event("assistant/message", {}))
        deadline = time.time() + 5.0
        while time.time() < deadline and not any(
            kind == "summary" for kind, _ in sink.bubbles
        ):
            integration.drain_events()
            time.sleep(0.05)
        self.assertIn(("summary", "总结：已压缩的回复要点"), sink.bubbles)
        # The full reply still bubbles as before.
        self.assertIn(("assistant", LONG_REPLY), sink.bubbles)

    def test_short_reply_gets_no_summary(self) -> None:
        integration, sink = self._integration("短回复的总结")
        integration._last_bubble_at = -1e9
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": "收到喵～"}))
        integration._handle_event(self._session_event("assistant/message", {}))
        for _ in range(10):
            integration.drain_events()
            time.sleep(0.05)
        self.assertFalse(any(kind == "summary" for kind, _ in sink.bubbles))

    def test_reasoning_never_produces_a_summary(self) -> None:
        """The chain of thought is off-limits: thinking must not generate
        summary bubbles (the user's original complaint)."""
        integration, sink = self._integration("思考总结不该出现")
        integration._last_bubble_at = -1e9
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        integration._handle_event(
            self._chunk(1, 1, {"type": "reasoning-delta", "text": "先分析再决定" * 40})
        )
        integration._handle_event(
            self._chunk(1, 1, {"type": "block-end", "block": {"type": "reasoning", "text": "先分析再决定" * 40}})
        )
        for _ in range(10):
            integration.drain_events()
            time.sleep(0.05)
        self.assertFalse(any(kind == "summary" for kind, _ in sink.bubbles))
        self.assertIsNone(integration._reasoning_pending or None)

    def test_summary_restating_the_reply_is_dropped(self) -> None:
        integration, sink = self._integration("收到喵～")
        integration._last_bubble_at = -1e9
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": "收到喵～"}))
        integration._handle_event(self._session_event("assistant/message", {}))
        for _ in range(10):
            integration.drain_events()
            time.sleep(0.05)
        # Short reply: never even submitted (below the 60-char gate).
        self.assertFalse(any(kind == "summary" for kind, _ in sink.bubbles))


if __name__ == "__main__":
    unittest.main()