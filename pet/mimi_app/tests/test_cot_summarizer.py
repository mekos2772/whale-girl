"""CoT summarizer tests: local heuristics, DSH config resolution, integration
reasoning flow (all offline; LLM calls are faked)."""

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
from mimi_pet.state_machine import PetState  # noqa: E402


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
                self.assertEqual(summarizer.summarize("很长的思考过程" * 50), "结论：使用缓存。")
            finally:
                cot._http_json = original

            # Network failure -> local heuristic, no exception.
            def failing_http(url, body, api_key, timeout=15.0):
                raise OSError("offline")

            cot._http_json = failing_http
            try:
                summary = summarizer.summarize("思考 " * 100)
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


class ReasoningFlowTests(unittest.TestCase):
    def _make(self, result: str):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.cot = FakeCot(result)
        return integration

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

    def test_reasoning_summarized_via_event_queue(self) -> None:
        class Sink:
            def __init__(self):
                self.rows = {}

            def set_row(self, tag, role, text):
                self.rows[tag] = (role, text)

        integration = self._make("结论：改用缓存方案")
        sink = Sink()
        integration.sink = sink
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        integration._handle_event(
            self._chunk(1, 1, {"type": "reasoning-delta", "text": "分析网络。" * 40})
        )
        integration._handle_event(
            self._chunk(1, 1, {"type": "block-end", "block": {"type": "reasoning", "text": "分析网络。" * 40}})
        )
        deadline = time.time() + 5.0
        while time.time() < deadline:
            integration.drain_events()
            if any(role == "summary" and "结论" in text for role, text in sink.rows.values()):
                break
            time.sleep(0.05)
        self.assertTrue(
            any(role == "summary" and "结论：改用缓存方案" in text for role, text in sink.rows.values()),
            f"sink rows: {sink.rows}",
        )
        self.assertIsNone(integration._reasoning_pending or None)

    def test_short_reasoning_uses_local_summary(self) -> None:
        class Sink:
            def __init__(self):
                self.rows = {}

            def set_row(self, tag, role, text):
                self.rows[tag] = (role, text)

        integration = self._make("不应被调用")
        sink = Sink()
        integration.sink = sink
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        integration._handle_event(
            self._chunk(1, 1, {"type": "reasoning-delta", "text": "简单思考"})
        )
        integration._handle_event(self._chunk(1, 1, {"type": "block-end", "block": {"type": "reasoning", "text": "简单思考"}}))
        integration.drain_events()
        self.assertTrue(any("简单思考" in text for _, text in sink.rows.values()))

    def test_tool_call_folds_into_single_row(self) -> None:
        class Sink:
            def __init__(self):
                self.rows = {}

            def set_row(self, tag, role, text):
                self.rows[tag] = (role, text)

        integration = self._make("x")
        sink = Sink()
        integration.sink = sink
        integration._handle_event(
            DshEvent(method="session/event", rpc_id="x", payload={"sessionId": "s", "event": {"type": "tool/call", "data": {"name": "pwsh"}}})
        )
        self.assertEqual(sink.rows.get("tool_active"), ("tool", "Pwsh"))
        integration._handle_event(
            DshEvent(method="session/event", rpc_id="x", payload={"sessionId": "s", "event": {"type": "tool/result", "data": {}}})
        )
        self.assertEqual(sink.rows.get("tool_active"), ("progress", "完成"))

    def test_no_emoji_in_panel_texts(self) -> None:
        class Sink:
            def __init__(self):
                self.rows = {}
                self.messages = []
                self.progress = []

            def set_row(self, tag, role, text):
                self.rows[tag] = (role, text)

            def append_message(self, role, text):
                self.messages.append(text)

            def upsert_progress(self, text):
                self.progress.append(text)

        engine = make_engine()
        integration = DshIntegration(engine)
        sink = Sink()
        integration.sink = sink
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        integration._handle_event(
            self._chunk(1, 1, {"type": "reasoning-delta", "text": "思考" * 40})
        )
        integration._handle_event(self._chunk(1, 1, {"type": "block-end", "block": {"type": "reasoning", "text": "思考" * 40}}))
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "text"}))
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": "好的"}))
        integration._handle_event(
            DshEvent(method="session/event", rpc_id="x", payload={"sessionId": "s", "event": {"type": "tool/call", "data": {"name": "pwsh"}}})
        )
        integration.drain_events()
        all_texts = list(sink.rows.values()) + sink.messages + sink.progress
        emoji = "🧠🤖🔧⏳✔✖🔐✅⚠️❓ℹ️🚫📡💬"
        for role, text in sink.rows.values():
            self.assertFalse(any(c in text for c in emoji), text)
            self.assertFalse(any(c in role for c in emoji), role)
        for text in sink.messages + sink.progress:
            self.assertFalse(any(c in text for c in emoji), text)


class DshAnimationTests(unittest.TestCase):
    """Dynamic switching of harness actions based on DSH activity."""

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

    def _make(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.cot = FakeCot("x")
        return engine, integration

    def test_reasoning_starts_thinking_animation(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        self.assertEqual(integration._dsh_anim, "thinking")
        self.assertIs(engine.states.state, PetState.PERFORMING)

    def test_tool_call_switches_away_from_thinking(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        self.assertEqual(integration._dsh_anim, "thinking")
        integration._handle_event(self._session_event("tool/call", {"name": "pwsh"}))
        self.assertEqual(integration._dsh_anim, "tool")
        self.assertTrue(integration._tool_active)
        self.assertIs(engine.states.state, PetState.PERFORMING)
        integration._handle_event(self._session_event("tool/result", {}))
        self.assertIsNone(integration._dsh_anim)
        self.assertFalse(integration._tool_active)

    def test_text_delta_stops_animation(self) -> None:
        _, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        self.assertEqual(integration._dsh_anim, "thinking")
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": "好的"}))
        self.assertIsNone(integration._dsh_anim)

    def test_maintain_anim_replays_while_condition_holds(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        # Let the current performance run to completion.
        now = time.time()
        for _ in range(600):
            engine.tick(now, 0.02, (0.0, 0.0), None, None)
            if engine.states.state is PetState.IDLE:
                break
        self.assertIs(engine.states.state, PetState.IDLE)
        integration.maintain_anim()
        self.assertIs(engine.states.state, PetState.PERFORMING)
        # Reasoning finished -> no replay.
        integration._reasoning_pending = False
        for _ in range(600):
            engine.tick(now, 0.02, (0.0, 0.0), None, None)
            if engine.states.state is PetState.IDLE:
                break
        integration.maintain_anim()
        self.assertIs(engine.states.state, PetState.IDLE)


if __name__ == "__main__":
    unittest.main()
