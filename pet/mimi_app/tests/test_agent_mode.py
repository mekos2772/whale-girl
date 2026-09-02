"""Pet-agent shadow session: creation, reuse, routing and mode switching."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.dsh_bridge import DshError, DshEvent, DshSession  # noqa: E402
from mimi_pet.dsh_integration import (  # noqa: E402
    AGENT_PERMISSION_PRESET,
    AGENT_PERSONA_PROMPT,
    AGENT_PERSONA_VERSION,
    AGENT_SESSION_TITLE,
    DshIntegration,
    DshQuestion,
)
from mimi_pet.engine import PetEngine  # noqa: E402


class FakeBridge:
    def __init__(self, existing=()) -> None:
        self.existing = list(existing)
        self.created: list = []
        self.archived: list = []
        self.renamed: list = []
        self.prompts: list = []
        self.responses: list = []
        self.preset_mutations: list = []  # (preset, expected_revision)
        self.preset = "workspace-write"
        self.revision = 3
        self.fail_create = False

    def list_sessions(self):
        return [DshSession(session_id=sid) for sid in self.existing]

    def create_session(self):
        if self.fail_create:
            raise DshError("dsh rpc session.create: probe: failed")
        sid = f"new-{len(self.created)}"
        # A fresh session inherits whatever default preset is active.
        self.created.append((sid, self.preset))
        self.existing.append(sid)
        return sid

    def archive_session(self, sid):
        self.archived.append(sid)

    def rename_session(self, sid, title):
        self.renamed.append((sid, title))

    def prompt(self, sid, text):
        self.prompts.append((sid, text))

    def settings_permission_preset(self):
        return self.preset, self.revision

    def respond(self, rpc_id, session_id, answers):
        self.responses.append((rpc_id, session_id, answers))

    def settings_set_permission_preset(self, preset, expected_revision):
        self.preset_mutations.append((preset, expected_revision))
        self.preset = preset
        self.revision = expected_revision + 1
        return self.revision


def _integration(tmp: Path):
    config = load_config()
    engine = PetEngine(ActionLibrary(Path(config["asset_manifest"])), config)
    integration = DshIntegration(engine)
    integration._agent_session_file = tmp / "agent_session.json"
    bridge = FakeBridge()
    integration.bridge = bridge
    return integration, bridge


class AgentModeTests(unittest.TestCase):
    def test_ensure_creates_full_access_archived_titled_persona(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.ensure_agent_session())
            sid = integration._agent_session_id
            self.assertIsNotNone(sid)
            self.assertEqual([c[0] for c in bridge.created], [sid])
            # The new session inherits the full-access preset...
            self.assertEqual(bridge.created[0][1], AGENT_PERMISSION_PRESET)
            # ...because the default was flipped around the create call.
            self.assertEqual(
                bridge.preset_mutations,
                [(AGENT_PERMISSION_PRESET, 3), ("workspace-write", 4)],
            )
            self.assertEqual(bridge.preset, "workspace-write")  # 用户默认被还原
            self.assertEqual(bridge.archived, [sid])
            self.assertEqual(bridge.renamed, [(sid, AGENT_SESSION_TITLE)])
            self.assertEqual(bridge.prompts, [(sid, AGENT_PERSONA_PROMPT)])
            stored = json.loads(integration._agent_session_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["session_id"], sid)
            self.assertEqual(stored["persona_version"], AGENT_PERSONA_VERSION)

    def test_ensure_restore_survives_create_failure(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            bridge.fail_create = True
            self.assertFalse(integration.ensure_agent_session())
            self.assertEqual(bridge.preset, "workspace-write")  # 仍还原了默认

    def test_ensure_reuses_stored_and_upgrades_persona(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            integration._agent_session_file.write_text(
                json.dumps({"session_id": "old-1", "persona_version": 0}), encoding="utf-8"
            )
            bridge.existing.append("old-1")
            self.assertTrue(integration.ensure_agent_session())
            self.assertEqual(integration._agent_session_id, "old-1")
            self.assertIn(("old-1", AGENT_PERSONA_PROMPT), bridge.prompts)
            self.assertEqual(bridge.created, [])  # 复用，不新建
            stored = json.loads(integration._agent_session_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["persona_version"], AGENT_PERSONA_VERSION)
            bridge.prompts.clear()
            self.assertTrue(integration.ensure_agent_session())  # 升级后不再重种
            self.assertEqual(bridge.prompts, [])

    def test_set_mode_agent_failure_keeps_link(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            bridge.fail_create = True
            self.assertFalse(integration.set_mode("agent"))
            self.assertEqual(integration.mode, "link")
            bridge.fail_create = False
            self.assertTrue(integration.set_mode("agent"))
            self.assertEqual(integration.mode, "agent")
            self.assertTrue(integration.set_mode("link"))
            self.assertEqual(integration.mode, "link")

    def test_panel_visibility_rule_is_mode_aware(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            integration.connected = True
            self.assertFalse(integration.should_show_panel())

            integration.working = True
            self.assertTrue(integration.should_show_panel())

            integration.working = False
            self.assertFalse(integration.should_show_panel())
            integration.set_mode("agent")
            self.assertFalse(integration.should_show_panel())
            self.assertTrue(integration.should_show_panel(head_hovered=True))
            self.assertTrue(integration.should_show_panel(panel_interacting=True))

    def test_disconnect_clears_questions_and_notifies_visibility(self) -> None:
        integration, _ = _integration(Path(tempfile.mkdtemp()))
        notified: list[bool] = []
        integration.on_activity = lambda: notified.append(True)
        integration.connected = True
        integration._active_session = "s-1"
        integration._waiting_text = "等待确认"
        integration.pending_questions.append(
            type("Question", (), {"rpc_id": "q-1"})()
        )
        integration._handle_event(
            DshEvent(method="__link", rpc_id="", payload={"connected": False})
        )
        self.assertFalse(integration.connected)
        self.assertIsNone(integration._active_session)
        self.assertFalse(integration.pending_questions)
        self.assertEqual(notified, [True])

    def test_prompt_routes_to_shadow_session_in_agent_mode(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            integration.set_mode("agent")
            sid = integration._agent_session_id
            self.assertTrue(integration.prompt_active("你好"))
            agent_prompts = [p for p in bridge.prompts if p[0] == sid and p[1] != AGENT_PERSONA_PROMPT]
            self.assertEqual(len(agent_prompts), 1)
            self.assertTrue(agent_prompts[0][1].startswith("[MIMI_RUNTIME_CONTEXT]"))
            self.assertIn("用户消息：\n你好", agent_prompts[0][1])
            persona_prompts = [p for p in bridge.prompts if p[1] == AGENT_PERSONA_PROMPT]
            self.assertEqual(persona_prompts, [(sid, AGENT_PERSONA_PROMPT)])  # 人格只种一次

    def test_link_prompt_is_sent_without_relationship_context(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            integration.connected = True
            integration._active_session = "normal"
            self.assertTrue(integration.prompt_active("工作内容"))
            self.assertIn(("normal", "工作内容"), bridge.prompts)
            self.assertNotIn("MIMI_RUNTIME_CONTEXT", bridge.prompts[-1][1])

    def test_link_events_do_not_change_affection(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            integration.connected = True
            integration._active_session = "normal"
            before = integration.engine.affection_score
            integration._handle_event(
                DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": "normal",
                        "event": {
                            "type": "assistant/chunk",
                            "data": {"chunk": {"type": "text-delta", "text": "工作回复"}},
                        },
                    },
                )
            )
            integration._handle_event(
                DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": "normal",
                        "event": {"type": "assistant/message", "data": {}},
                    },
                )
            )
            integration._handle_event(
                DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": "normal",
                        "event": {"type": "tool/result", "data": {"error": "失败"}},
                    },
                )
            )
            self.assertEqual(integration.engine.affection_score, before)

    def test_agent_mode_filters_events_to_shadow_session(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            integration.set_mode("agent")
            sid = integration._agent_session_id
            self.assertTrue(integration._accept_event_session({"sessionId": sid}))
            self.assertFalse(integration._accept_event_session({"sessionId": "other"}))

    def test_agent_chat_input_changes_affection_once_and_reply_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.set_mode("agent"))
            sid = integration._agent_session_id
            before = integration.engine.affection_score
            self.assertTrue(integration.prompt_active("谢谢你，辛苦啦"))
            self.assertEqual(integration.engine.affection_score, before + 1)
            self.assertIn("用户消息：\n谢谢你，辛苦啦", bridge.prompts[-1][1])

            def event(event_type, data):
                return DshEvent(
                    method="session/event",
                    rpc_id="x",
                    payload={
                        "sessionId": sid,
                        "event": {"type": event_type, "data": data},
                    },
                )

            integration._handle_event(
                event("assistant/chunk", {"chunk": {"type": "text-delta", "text": "你好呀"}})
            )
            integration._handle_event(event("assistant/message", {}))
            self.assertEqual(integration.engine.affection_score, before + 1)
            integration._handle_event(event("assistant/message", {}))
            self.assertEqual(integration.engine.affection_score, before + 1)

    def test_agent_chat_rejects_non_chat_and_duplicate_input(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            self.assertTrue(integration.set_mode("agent"))
            before = integration.engine.affection_score
            self.assertTrue(integration.prompt_active("/help"))
            self.assertEqual(integration.engine.affection_score, before)
            self.assertTrue(integration.prompt_active("好好好"))
            self.assertEqual(integration.engine.affection_score, before)
            self.assertTrue(integration.prompt_active("你好"))
            self.assertEqual(integration.engine.affection_score, before + 1)
            self.assertTrue(integration.prompt_active("你好"))
            self.assertEqual(integration.engine.affection_score, before + 1)

    def test_agent_question_answer_does_not_add_separate_affection(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.set_mode("agent"))
            sid = integration._agent_session_id
            before = integration.engine.affection_score
            question = DshQuestion(
                rpc_id="q-rpc",
                session_id=sid,
                id="q1",
                question="继续吗？",
            )
            integration.pending_questions.append(question)
            self.assertTrue(integration.answer_question(question, ["继续"]))
            self.assertEqual(integration.engine.affection_score, before)

    def test_failed_agent_chat_does_not_change_affection(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.set_mode("agent"))
            before = integration.engine.affection_score
            original_prompt = bridge.prompt

            def fail_prompt(sid, text):
                raise DshError("发送失败")

            bridge.prompt = fail_prompt
            self.assertFalse(integration.prompt_active("谢谢你"))
            self.assertEqual(integration.engine.affection_score, before)
            bridge.prompt = original_prompt

        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            integration.connected = True
            integration._active_session = "normal"
            before = integration.engine.affection_score
            self.assertTrue(integration.prompt_active("谢谢你，辛苦啦"))
            self.assertEqual(integration.engine.affection_score, before)

    def test_agent_poll_never_follows_a_normal_running_session(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.set_mode("agent"))
            shadow = integration._agent_session_id
            integration._apply_sessions(
                [DshSession(session_id="normal", running=True, title="普通项目")]
            )
            self.assertEqual(integration._active_session, shadow)
            self.assertFalse(integration.working)

    def test_link_mode_hides_and_never_autofollows_shadow_session(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            self.assertTrue(integration.ensure_agent_session())
            shadow = integration._agent_session_id
            integration._apply_sessions(
                [
                    DshSession(session_id=shadow, running=True, title="Mimi 管家"),
                    DshSession(session_id="normal", running=True, title="普通项目"),
                ]
            )
            self.assertEqual(integration._active_session, "normal")
            self.assertNotIn(shadow, [item["session_id"] for item in integration.session_choices()])
            integration._active_session = None
            self.assertFalse(integration._accept_event_session({"sessionId": shadow}))

    def test_permission_restore_retries_after_revision_conflict(self) -> None:
        class ConflictBridge(FakeBridge):
            def __init__(self) -> None:
                super().__init__()
                self.failed_restore = False

            def settings_set_permission_preset(self, preset, expected_revision):
                if preset == "workspace-write" and not self.failed_restore:
                    self.failed_restore = True
                    self.revision += 1
                    raise DshError("revision conflict")
                return super().settings_set_permission_preset(preset, expected_revision)

        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            bridge = ConflictBridge()
            integration.bridge = bridge
            self.assertTrue(integration.ensure_agent_session())
            self.assertTrue(bridge.failed_restore)
            self.assertEqual(bridge.preset, "workspace-write")


if __name__ == "__main__":
    unittest.main()
