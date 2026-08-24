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
from mimi_pet.dsh_bridge import DshError, DshSession  # noqa: E402
from mimi_pet.dsh_integration import (  # noqa: E402
    AGENT_PERMISSION_PRESET,
    AGENT_PERSONA_PROMPT,
    AGENT_PERSONA_VERSION,
    AGENT_SESSION_TITLE,
    DshIntegration,
)
from mimi_pet.engine import PetEngine  # noqa: E402


class FakeBridge:
    def __init__(self, existing=()) -> None:
        self.existing = list(existing)
        self.created: list = []
        self.archived: list = []
        self.renamed: list = []
        self.prompts: list = []
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

    def test_prompt_routes_to_shadow_session_in_agent_mode(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, bridge = _integration(Path(t))
            integration.set_mode("agent")
            sid = integration._agent_session_id
            self.assertTrue(integration.prompt_active("你好"))
            self.assertIn((sid, "你好"), bridge.prompts)
            persona_prompts = [p for p in bridge.prompts if p[1] == AGENT_PERSONA_PROMPT]
            self.assertEqual(persona_prompts, [(sid, AGENT_PERSONA_PROMPT)])  # 人格只种一次

    def test_agent_mode_filters_events_to_shadow_session(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            integration, _ = _integration(Path(t))
            integration.set_mode("agent")
            sid = integration._agent_session_id
            self.assertTrue(integration._accept_event_session({"sessionId": sid}))
            self.assertFalse(integration._accept_event_session({"sessionId": "other"}))


if __name__ == "__main__":
    unittest.main()
