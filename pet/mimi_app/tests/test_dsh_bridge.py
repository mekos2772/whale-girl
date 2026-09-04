"""DSH bridge tests: frame parsing and live RPC (skipped when DSH is down)."""

from __future__ import annotations

import json
import queue
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.dsh_bridge import DshBridge, DshError, DshEvent, DshEventThread  # noqa: E402
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.state_machine import PetState  # noqa: E402


def dsh_available() -> bool:
    try:
        DshBridge(host="127.0.0.1:3080", timeout=0.5).rpc("session/list", {"_request": {}})
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
        # Session ids are opaque: older DSH versions produced raw UUIDs,
        # newer ones use the "session-" prefix. Only the id's identity
        # matters to the pet (it never parses the prefix).
        self.assertTrue(all(isinstance(s.session_id, str) and s.session_id for s in sessions))

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


def fake_session(sid, running=False, title="", cwd="C:/proj") -> object:
    return type(
        "S",
        (),
        {
            "session_id": sid,
            "running": running,
            "agent_preset": "standard",
            "title": title,
            "cwd": cwd,
            "steps": 3,
        },
    )()


class ProjectSelectionTests(unittest.TestCase):
    """Multi-project support: pin which DSH session the capsule tracks."""

    def test_autofollow_picks_first_running_session(self) -> None:
        integration = DshIntegration(make_engine())
        integration._apply_sessions(
            [fake_session("s-a", running=True, title="任务A"),
             fake_session("s-b", running=True, title="任务B")]
        )
        self.assertEqual(integration._active_session, "s-a")
        self.assertIn("任务A", integration.activity.full_activity)

    def test_pinned_session_wins_over_first_running(self) -> None:
        integration = DshIntegration(make_engine())
        integration.select_session("s-b")
        self.assertEqual(integration.pinned_session(), "s-b")
        integration._apply_sessions(
            [fake_session("s-a", running=True, title="任务A"),
             fake_session("s-b", running=True, title="任务B")]
        )
        self.assertEqual(integration._active_session, "s-b")
        self.assertIn("任务B", integration.activity.full_activity)
        # A pinned non-running project is still displayed (chosen project).
        integration._apply_sessions(
            [fake_session("s-a", running=True, title="任务A"),
             fake_session("s-b", running=False, title="任务B")]
        )
        self.assertEqual(integration._active_session, "s-b")
        self.assertFalse(integration.working)
        self.assertEqual(integration.engine.player.action.id, "celebrate")

    def test_unpin_returns_to_autofollow(self) -> None:
        integration = DshIntegration(make_engine())
        integration.select_session("s-b")
        integration.select_session("")
        self.assertIsNone(integration.pinned_session())
        integration._apply_sessions(
            [fake_session("s-a", running=True, title="任务A"),
             fake_session("s-b", running=True, title="任务B")]
        )
        self.assertEqual(integration._active_session, "s-a")

    def test_missing_pinned_session_never_falls_through_to_another_project(self) -> None:
        integration = DshIntegration(make_engine())
        integration.select_session("s-pinned")
        integration._apply_sessions([fake_session("s-other", running=True, title="别的任务")])
        self.assertEqual(integration._active_session, "s-pinned")
        self.assertFalse(integration.working)

    def test_session_switch_retires_previous_stream_state(self) -> None:
        integration = DshIntegration(make_engine())
        integration._apply_sessions([fake_session("s-a", running=True, title="任务A")])
        integration._text = "旧回复"
        integration._tool_active = True
        integration._reasoning_pending = True
        generation = integration._view_generation
        integration.select_session("s-b")
        integration._apply_sessions([fake_session("s-b", running=False, title="任务B")])
        self.assertGreater(integration._view_generation, generation)
        self.assertEqual(integration._text, "")
        self.assertFalse(integration._tool_active)
        self.assertFalse(integration._reasoning_pending)

    def test_session_choices_labels_project_and_title(self) -> None:
        integration = DshIntegration(make_engine())
        integration._known_sessions = [
            fake_session("s-1", running=True, title="构建桌宠", cwd="C:/p/桌宠")
        ]
        choices = integration.session_choices()
        self.assertEqual(choices[0]["session_id"], "s-1")
        self.assertIn("桌宠", choices[0]["label"])
        self.assertTrue(choices[0]["running"])


class LinkHandoffTests(unittest.TestCase):
    """The websocket thread must never touch Qt: link state is handed off via
    the event queue and applied on the main thread ('__link' event)."""

    def test_queue_link_only_posts_marker_event(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        integration._queue_link(True)
        event = integration.event_queue.get_nowait()
        self.assertEqual(event.method, "__link")
        self.assertTrue(event.payload["connected"])
        self.assertTrue(integration.event_queue.empty())

    def test_link_event_applied_on_main_thread(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        revealed: list[bool] = []
        integration.on_activity = lambda: revealed.append(True)
        integration._handle_event(
            DshEvent(method="__link", rpc_id="", payload={"connected": True})
        )
        self.assertTrue(integration.connected)
        self.assertTrue(integration._capsule_revealed)
        self.assertEqual(revealed, [True])
        integration._handle_event(
            DshEvent(method="__link", rpc_id="", payload={"connected": False})
        )
        self.assertFalse(integration.connected)

    def test_disconnect_retires_live_state_and_never_reports_connected(self) -> None:
        integration = DshIntegration(make_engine())
        integration.connected = True
        integration._active_session = "s"
        integration.working = True
        integration._tool_active = True
        integration._reasoning_pending = True
        integration._handle_event(
            DshEvent(method="__link", rpc_id="", payload={"connected": False})
        )
        self.assertFalse(integration.working)
        self.assertFalse(integration._tool_active)
        self.assertFalse(integration._reasoning_pending)
        self.assertFalse(integration.activity.is_connected)


class EventThreadLifecycleTests(unittest.TestCase):
    def test_reader_emits_link_transitions_and_decodes_one_frame(self) -> None:
        events = queue.Queue()
        transitions: list[str] = []
        reader = DshEventThread(
            events=events,
            on_connect=lambda: transitions.append("up"),
            on_disconnect=lambda: transitions.append("down"),
        )

        class FakeSocket:
            def __init__(self) -> None:
                self.reads = 0
                self.closed = False

            def settimeout(self, value) -> None:
                self.timeout = value

            def recv(self):
                self.reads += 1
                if self.reads == 1:
                    return json.dumps(
                        {
                            "type": "server-request",
                            "rpcId": "rpc-1",
                            "method": "session/event",
                            "payload": {"sessionId": "s"},
                        }
                    )
                reader._stop.set()
                raise OSError("closed")

            def close(self) -> None:
                self.closed = True

        socket = FakeSocket()
        websocket_module = types.SimpleNamespace(create_connection=lambda *a, **kw: socket)
        with mock.patch.dict(sys.modules, {"websocket": websocket_module}):
            reader._run()

        event = events.get_nowait()
        self.assertEqual((event.method, event.rpc_id), ("session/event", "rpc-1"))
        self.assertEqual(transitions, ["up", "down"])
        self.assertTrue(socket.closed)


class StreamingSinkTests(unittest.TestCase):
    def test_events_from_unselected_project_are_ignored(self) -> None:
        engine = make_engine()
        integration = DshIntegration(engine)
        sink = FakeSink()
        integration.sink = sink
        integration.select_session("s-a")
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s-b",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {
                            "turn": 1,
                            "step": 1,
                            "chunk": {"type": "text-delta", "text": "不该出现"},
                        },
                    },
                },
            )
        )
        self.assertEqual(sink.rows, {})
        self.assertEqual(integration.pinned_session(), "s-a")

    def test_jobs_from_unselected_project_are_ignored(self) -> None:
        integration = DshIntegration(make_engine())
        sink = FakeSink()
        integration.sink = sink
        integration.select_session("s-a")
        integration._handle_event(
            DshEvent(
                method="session/jobs",
                rpc_id="x",
                payload={"sessionId": "s-b", "jobs": [{"status": "failed"}]},
            )
        )
        self.assertEqual(sink.messages, [])

    def test_late_summary_from_previous_project_is_dropped(self) -> None:
        integration = DshIntegration(make_engine())
        sink = FakeSink()
        integration.sink = sink
        integration.select_session("s-a")
        old_generation = integration._view_generation
        integration.select_session("s-b")
        integration._handle_event(
            DshEvent(
                method="cot/result",
                rpc_id="",
                payload={
                    "tag": "old",
                    "text": "旧项目总结",
                    "generation": old_generation,
                    "sessionId": "s-a",
                },
            )
        )
        self.assertNotIn("old", sink.rows)

    def test_event_drain_has_a_ui_budget(self) -> None:
        integration = DshIntegration(make_engine())
        seen = []
        integration._handle_event = seen.append
        for index in range(integration.MAX_EVENTS_PER_DRAIN + 20):
            integration.event_queue.put(DshEvent(method=str(index), rpc_id="", payload={}))
        integration.drain_events()
        self.assertEqual(len(seen), integration.MAX_EVENTS_PER_DRAIN)
        self.assertEqual(integration.event_queue.qsize(), 20)

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
        self.assertTrue(engine.talking)
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {
                        "type": "assistant/chunk",
                        "data": {"turn": 1, "step": 1, "chunk": {"type": "finish"}},
                    },
                },
            )
        )
        self.assertFalse(engine.talking)

    def test_live_turn_events_update_working_without_waiting_for_poll(self) -> None:
        integration = DshIntegration(make_engine())
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={
                    "sessionId": "s",
                    "event": {"type": "turn/start", "data": {"turn": 1, "step": 0}},
                },
            )
        )
        self.assertTrue(integration.working)
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="x",
                payload={"sessionId": "s", "event": {"type": "turn/end", "data": {}}},
            )
        )
        self.assertFalse(integration.working)

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
        self.assertTrue(any("Pwsh" in text for _, text in sink.messages))

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


class NewRemoteProtocolTests(unittest.TestCase):
    def test_rpc_uses_slash_endpoint_args_and_validates_response(self) -> None:
        bridge = DshBridge(timeout=0.1)
        captured = {}

        def post(path, body):
            captured["path"] = path
            captured["body"] = body
            return {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": {"accepted": True}},
            }

        bridge._post = post
        value = bridge.rpc("session.prompt", {"request": {"sessionId": "s"}})
        self.assertEqual(value, {"accepted": True})
        self.assertEqual(captured["path"], "/api/session/prompt")
        self.assertEqual(captured["body"]["method"], "session/prompt")
        self.assertEqual(captured["body"]["payload"], {"args": {"request": {"sessionId": "s"}}})

    def test_rpc_rejects_wrong_response_type_or_id(self) -> None:
        bridge = DshBridge(timeout=0.1)
        bridge._post = lambda path, body: {
            "type": "client-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": {}},
        }
        with self.assertRaises(DshError):
            bridge.rpc("session/list", {"_request": {}})

        bridge._post = lambda path, body: {
            "type": "server-response",
            "rpcId": "other",
            "result": {"ok": True, "value": {}},
        }
        with self.assertRaises(DshError):
            bridge.rpc("session/list", {"_request": {}})

    def test_settings_mutate_keeps_three_wire_arguments(self) -> None:
        bridge = DshBridge(timeout=0.1)
        captured = {}

        def post(path, body):
            captured["body"] = body
            return {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": {"revision": 8}},
            }

        bridge._post = post
        self.assertEqual(bridge.settings_set_permission_preset("danger-full-access", 7), 8)
        self.assertEqual(
            captured["body"]["payload"]["args"],
            {
                "ns": "permission",
                "ops": [{"op": "set", "path": ["defaultPreset"], "value": "danger-full-access"}],
                "expectedRevision": 7,
            },
        )

    def test_remote_event_result_uses_client_and_event_ids(self) -> None:
        bridge = DshBridge(timeout=0.1)
        captured = {}

        def post(path, body):
            captured["path"] = path
            captured["args"] = body["payload"]["args"]
            return {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": None},
            }

        bridge._post = post
        bridge.respond("client-1", "event-1", {"answers": [{"id": "q", "selected": ["是"]}]})
        self.assertEqual(captured["path"], "/api/$events/result")
        self.assertEqual(captured["args"]["clientId"], "client-1")
        self.assertEqual(captured["args"]["eventId"], "event-1")
        self.assertEqual(captured["args"]["outcome"]["kind"], "result")

    def test_follow_decoder_accepts_snapshot_event_chunks_and_deduplicates_seq(self) -> None:
        events = queue.Queue()
        reader = DshEventThread(events=events)
        reader._decode_follow_value(
            "session-1",
            {
                "type": "snapshot",
                "cursor": 3,
                "records": [
                    {
                        "type": "event",
                        "event": {"type": "turn/start", "seq": 1, "time": 1, "data": {}},
                    },
                    {
                        "type": "chunks",
                        "event": {
                            "type": "chunkrow/text-chunks",
                            "seq": 2,
                            "time": 2,
                            "data": {"texts": ["你", "好"], "turn": 1, "step": 1},
                        },
                    },
                ],
                "hasMore": False,
                "projections": {"asOfSeq": 3, "values": {}},
            },
        )
        reader._decode_follow_value(
            "session-1",
            {"type": "event", "event": {"type": "turn/start", "seq": 1, "time": 1, "data": {}}},
        )
        decoded = [events.get_nowait() for _ in range(events.qsize())]
        self.assertEqual([event.method for event in decoded], ["session/event", "session/event", "session/event"])
        self.assertEqual(decoded[1].payload["event"]["data"]["chunk"]["text"], "你")
        self.assertEqual(decoded[2].payload["event"]["data"]["chunk"]["text"], "好")

    def test_waterfall_decoder_preserves_client_and_event_identity(self) -> None:
        events = queue.Queue()
        reader = DshEventThread(events=events)
        reader._client_id = "client-2"
        reader._decode_event_value(
            {
                "type": "waterfall",
                "event": "user-questions/request",
                "eventId": "event-2",
                "agentId": "session-2",
                "request": {"questions": []},
            }
        )
        event = events.get_nowait()
        self.assertEqual(event.client_id, "client-2")
        self.assertEqual(event.event_id, "event-2")
        self.assertEqual(event.rpc_id, "event-2")


class BubbleDuplicateTests(unittest.TestCase):
    """Regression: previous replies must not replay as delayed bubbles."""

    def _integration(self):
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        integration = DshIntegration(engine)

        class BubbleSink:
            def __init__(self) -> None:
                self.bubbles: list[tuple[str, str]] = []

            def show_bubble(self, text, kind="assistant"):
                self.bubbles.append((kind, text))

            def set_row(self, *a):
                pass

            def append_message(self, *a):
                pass

            def upsert_progress(self, *a):
                pass

            def append_question(self, *a):
                pass

            def set_status(self, *a):
                pass

            def show_panel(self):
                pass

        sink = BubbleSink()
        integration.sink = sink
        return integration, sink

    def _event(self, event_type, data):
        return DshEvent(
            method="session/event",
            rpc_id="x",
            payload={"sessionId": "s", "event": {"type": event_type, "data": data}},
        )

    def test_next_turn_does_not_replay_previous_reply(self) -> None:
        integration, sink = self._integration()
        integration._handle_event(
            self._event("assistant/chunk", {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "第一条回复"}})
        )
        integration._handle_event(self._event("assistant/message", {}))
        # New turn: user message, then a tool-only assistant step fires
        # assistant/message with no text of its own.
        integration._handle_event(self._event("user/message", {}))
        integration._handle_event(self._event("tool/call", {"name": "pwsh", "arguments": "{}"}))
        integration._handle_event(self._event("assistant/message", {}))
        texts = [t for _, t in sink.bubbles]
        self.assertEqual(texts.count("第一条回复"), 1)


class ReasoningFlowTests(unittest.TestCase):
    """Reasoning chunks drive the thinking state; sink rows stay emoji-free."""

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

    def test_tool_call_folds_into_single_row(self) -> None:
        class Sink:
            def __init__(self):
                self.rows = {}

            def set_row(self, tag, role, text):
                self.rows[tag] = (role, text)

        integration = DshIntegration(make_engine())
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

        integration = DshIntegration(make_engine())
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
        emoji = "🧠🤖🔧⏳✔✖🔐✅⚠️❓ℹ️🚫📡💬"
        for role, text in sink.rows.values():
            self.assertFalse(any(c in text for c in emoji), text)
            self.assertFalse(any(c in role for c in emoji), role)
        for text in sink.messages + sink.progress:
            self.assertFalse(any(c in text for c in emoji), text)


class HarnessAnimationTests(unittest.TestCase):
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
        return engine, integration

    def test_reasoning_starts_thinking_animation(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        self.assertEqual(integration._dsh_anim, "thinking")
        self.assertIs(engine.states.state, PetState.PERFORMING)

    def test_reasoning_block_end_retires_thinking_state(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        integration._handle_event(
            self._chunk(1, 1, {"type": "reasoning-delta", "text": "思考" * 40})
        )
        self.assertTrue(integration._reasoning_pending)
        integration._handle_event(
            self._chunk(1, 1, {"type": "block-end", "block": {"type": "reasoning", "text": "思考" * 40}})
        )
        self.assertFalse(integration._reasoning_pending)
        self.assertEqual(integration._reasoning, "")
        self.assertIsNone(integration._reasoning_tag)

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
        self.assertIs(engine.states.state, PetState.IDLE)

    def test_text_delta_stops_animation(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        self.assertEqual(integration._dsh_anim, "thinking")
        integration._handle_event(self._chunk(1, 1, {"type": "text-delta", "text": "好的"}))
        self.assertIsNone(integration._dsh_anim)
        self.assertIs(engine.states.state, PetState.IDLE)

    def test_harness_does_not_interrupt_direct_user_action(self) -> None:
        engine, integration = self._make()
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.player.action.id, "head_pat")
        integration._handle_event(
            self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"})
        )
        self.assertEqual(integration._dsh_anim, "thinking")
        self.assertEqual(engine.player.action.id, "head_pat")

    def test_done_cancels_work_animation_then_celebrates(self) -> None:
        engine, integration = self._make()
        integration._handle_event(
            self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"})
        )
        integration._on_done()
        self.assertIsNone(integration._dsh_anim)
        self.assertIs(engine.states.state, PetState.PERFORMING)
        self.assertEqual(engine.player.action.id, "celebrate")

    def test_maintain_anim_replays_with_gap_while_condition_holds(self) -> None:
        engine, integration = self._make()
        integration._handle_event(self._chunk(1, 1, {"type": "block-start", "blockType": "reasoning"}))
        # Let the current performance run to completion.
        now = time.time()
        for _ in range(600):
            engine.tick(now, 0.02, (0.0, 0.0), None, None)
            if engine.states.state is PetState.IDLE:
                break
        self.assertIs(engine.states.state, PetState.IDLE)
        # Throttled: an immediate replay is suppressed so the pet rests in its
        # natural idle sway between work cycles.
        integration.maintain_anim()
        self.assertIs(engine.states.state, PetState.IDLE)
        # Once the replay gap has passed the work animation replays (condition
        # still holds: reasoning pending).
        integration._anim_last_replay = time.perf_counter() - (integration.REPLAY_GAP_S + 1.0)
        integration.maintain_anim()
        self.assertIs(engine.states.state, PetState.PERFORMING)
        # Reasoning finished -> no replay.
        integration._reasoning_pending = False
        for _ in range(600):
            engine.tick(now, 0.02, (0.0, 0.0), None, None)
            if engine.states.state is PetState.IDLE:
                break
        integration._anim_last_replay = time.perf_counter() - (integration.REPLAY_GAP_S + 1.0)
        integration.maintain_anim()
        self.assertIs(engine.states.state, PetState.IDLE)


if __name__ == "__main__":
    unittest.main()
