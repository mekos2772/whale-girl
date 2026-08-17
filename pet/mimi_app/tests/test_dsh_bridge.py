"""DSH bridge tests: frame parsing and live RPC (skipped when DSH is down)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.dsh_bridge import DshBridge, DshError, DshEvent  # noqa: E402
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.action_library import ActionLibrary  # noqa: E402


def dsh_available() -> bool:
    try:
        DshBridge(host="127.0.0.1:3080").rpc("session.list", {})
        return True
    except Exception:
        return False


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


class QuestionParsingTests(unittest.TestCase):
    def test_question_requested_frame_parses(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        event = DshEvent(
            method="question/requested",
            rpc_id="q-1",
            payload={
                "sessionId": "session-abc",
                "questions": [
                    {
                        "id": "mode",
                        "question": "选哪种模式？",
                        "header": "模式选择",
                        "options": [{"label": "自动", "description": "推荐"}, {"label": "手动"}],
                        "multiSelect": False,
                    }
                ],
            },
        )
        integration._handle_event(event)
        self.assertEqual(len(integration.pending_questions), 1)
        question = integration.pending_questions[0]
        self.assertEqual(question.rpc_id, "q-1")
        self.assertEqual(question.id, "mode")
        self.assertEqual(question.question, "选哪种模式？")
        self.assertEqual([opt["label"] for opt in question.options], ["自动", "手动"])

    def test_question_resolved_clears_pending(self) -> None:
        from mimi_pet.dsh_integration import DshQuestion

        engine = make_engine()
        integration = DshIntegration(engine)
        integration.pending_questions = [
            DshQuestion(rpc_id="q-1", session_id="s", id="a", question="q"),
            DshQuestion(rpc_id="q-2", session_id="s", id="b", question="q2"),
        ]
        integration._handle_event(
            DshEvent(method="question/resolved", rpc_id="x", payload={"questionRpcId": "q-1"})
        )
        self.assertEqual(len(integration.pending_questions), 1)
        self.assertEqual(integration.pending_questions[0].rpc_id, "q-2")

    def test_jobs_running_do_not_duplicate_tool_rows(self) -> None:
        class Sink:
            def __init__(self):
                self.progress = []
                self.messages = []

            def upsert_progress(self, text):
                self.progress.append(text)

            def append_message(self, role, text):
                self.messages.append(text)

        engine = make_engine()
        engine.place_at(500.0, 800.0)
        integration = DshIntegration(engine)
        integration.sink = Sink()
        integration._handle_event(
            DshEvent(
                method="session/jobs",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "jobs": [{"id": "j1", "kind": "pwsh", "label": "pip install pkg", "status": "running"}],
                },
            )
        )
        # Running jobs are already surfaced as tool rows: no duplicate progress.
        self.assertEqual(integration.sink.progress, [])
        self.assertEqual(integration.sink.messages, [])
        # Failed jobs still produce a message.
        integration._handle_event(
            DshEvent(
                method="session/jobs",
                rpc_id="x",
                payload={"sessionId": "s", "jobs": [{"id": "j1", "status": "failed"}]},
            )
        )
        self.assertTrue(any("失败" in m for m in integration.sink.messages))


@unittest.skipUnless(dsh_available(), "DSH web API is not reachable")
class LiveDshTests(unittest.TestCase):
    def test_session_list_live(self) -> None:
        sessions = DshBridge().list_sessions()
        self.assertIsInstance(sessions, list)
        if sessions:
            self.assertTrue(all(s.session_id.startswith("session-") for s in sessions))

    def test_question_answer_roundtrip_requires_live_question(self) -> None:
        # No pending question exists without an ask(); just verify the
        # bridge can reach the respond endpoint (error tolerated).
        bridge = DshBridge()
        try:
            bridge.respond("nonexistent-rpc", "session-x", [{"id": "x", "selected": []}])
        except DshError:
            pass  # expected: unknown rpc id


class FakeSink:
    """Records sink calls for streaming assertions."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}
        self.messages: list[tuple[str, str]] = []
        self.progress: list[str] = []
        self.questions: list = []
        self.shown = 0

    def set_row(self, tag, role, text):
        self.rows[tag] = (role, text)

    def append_message(self, role, text):
        self.messages.append((role, text))

    def upsert_progress(self, text):
        self.progress.append(text)

    def append_question(self, question):
        self.questions.append(question)

    def show_panel(self):
        self.shown += 1


class StreamingSinkTests(unittest.TestCase):
    def test_text_delta_streams_into_tagged_row(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        for text in ("你", "好", "！"):
            integration._handle_event(
                DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": "s",
                        "event": {
                            "type": "assistant/chunk",
                            "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": text}},
                        },
                    },
                )
            )
        self.assertEqual(sink.rows.get("text_1_1"), ("assistant", "你好！"))

    def test_block_start_resets_stream_row(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"turn": 1, "step": 1, "chunk": {"type": "block-start", "blockType": "text"}},
                    },
                },
            )
        )
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "第一段"}},
                    },
                },
            )
        )
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"turn": 1, "step": 1, "chunk": {"type": "block-start", "blockType": "text"}},
                    },
                },
            )
        )
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "第二段"}},
                    },
                },
            )
        )
        self.assertEqual(sink.rows.get("text_1"), ("assistant", "第一段"))
        self.assertEqual(sink.rows.get("text_2"), ("assistant", "第二段"))

    def test_tool_call_delta_streams_name(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        for piece in ("web_", "search"):
            integration._handle_event(
                DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": "s",
                        "event": {
                            "type": "assistant/chunk",
                            "data": {
                                "turn": 1,
                                "step": 2,
                                "chunk": {"type": "tool-call-delta", "name": piece},
                            },
                        },
                    },
                )
            )
        self.assertEqual(sink.rows.get("tool_active"), ("tool", "Search"))

    def test_question_forwards_to_sink(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        integration._handle_event(
            DshEvent(
                method="question/requested",
                rpc_id="q-9",
                payload={
                    "sessionId": "s",
                    "questions": [{"id": "m", "question": "继续吗？", "options": [{"label": "是"}]}],
                },
            )
        )
        self.assertEqual(len(sink.questions), 1)
        self.assertEqual(sink.questions[0].id, "m")
        self.assertEqual(len(integration.pending_questions), 1)

    def test_approval_forwards_to_sink(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        integration._handle_event(
            DshEvent(
                method="approval/requested",
                rpc_id="x",
                payload={"toolName": "pwsh", "reason": "运行命令"},
            )
        )
        self.assertTrue(any(role == "info" for role, _ in sink.messages))
        self.assertTrue(any("pwsh" in text for _, text in sink.messages))

    def test_poll_activity_callback(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        integration.on_activity = sink.show_panel
        integration._available_sessions = lambda: [
            type("S", (), {"running": True, "session_id": "session-1", "steps": 3})()
        ]
        integration.poll_status()
        self.assertTrue(integration.working)
        self.assertEqual(sink.shown, 1)
        integration._available_sessions = lambda: []
        integration.poll_status()
        self.assertFalse(integration.working)


if __name__ == "__main__":
    unittest.main()
