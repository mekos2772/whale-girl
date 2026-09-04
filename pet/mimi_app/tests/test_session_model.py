"""Unit tests for the three approved-plan features.

1. Session-target stability for ``prompt_active`` (link mode auto‑pick).
2. Bridge ``model_catalog`` / ``select_session_model`` parsing + idempotency.
3. Integration model picker (cache, refresh, error tolerance, model selection).
4. Qt menu surfacing the picker (independent from 摘要模型 / project menu).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

try:
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:
    HAS_QT = False

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.dsh_bridge import (  # noqa: E402
    DshBridge,
    DshError,
    DshSession,
    ModelCatalogEntry,
)
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402


# ---------------------------------------------------------------------------
# helpers


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


def fake_session(
    sid: str,
    running: bool = False,
    agent_preset: str = "",
    model_provider: str = "",
    model_id: str = "",
    reasoning_effort: str = "",
) -> DshSession:
    return DshSession(
        session_id=sid,
        running=running,
        agent_preset=agent_preset,
        title=f"项目 {sid[-2:]}",
        cwd=f"C:/p/{sid}",
        model_provider=model_provider,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
    )


class FakeBridge:
    """In-memory DshBridge replacement that records catalog/prompt events."""

    def __init__(self, catalog=("deepseek-chat", "deepseek-reasoner")) -> None:
        self._catalog = list(catalog)
        self.prompts: list[tuple[str, str]] = []
        self.model_selections: list[tuple[str, str, str, str | None]] = []
        self.catalog_calls = 0

    def list_sessions(self):
        return []

    def prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))

    def model_catalog(self):
        self.catalog_calls += 1
        return [
            ModelCatalogEntry(
                model_id=mid,
                label=mid,
                provider="deepseek",
                provider_label="DeepSeek",
            )
            for mid in self._catalog
        ]

    def select_session_model(
        self,
        session_id: str,
        model_id: str,
        provider: str,
        reasoning_effort: str | None = None,
    ):
        self.model_selections.append((session_id, provider, model_id, reasoning_effort))
        return {
            "selected": {
                "provider": provider,
                "model": model_id,
                **({"reasoningEffort": reasoning_effort} if reasoning_effort else {}),
            }
        }


# ---------------------------------------------------------------------------
# 1. session-target stability


class SessionTargetStabilityTests(unittest.TestCase):
    """prompt_active in link mode must deterministically pick exactly one
    target and stick with it across consecutive calls until DSH state changes.
    """

    def test_resolve_picks_first_running_when_no_pinned_no_active(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.bridge = FakeBridge()
        integration._available_sessions = lambda: [
            fake_session("s-a", running=True),
            fake_session("s-b", running=False),
            fake_session("s-c", running=True),
        ]
        target = integration.resolve_prompt_target()
        self.assertEqual(target, "s-a")
        self.assertEqual(integration.resolve_prompt_target(), "s-a")

    def test_resolve_prefers_pinned_session_even_when_idle(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.bridge = FakeBridge()
        integration._available_sessions = lambda: [
            fake_session("s-a", running=True),
            fake_session("s-b", running=False),
        ]
        integration.select_session("s-b")
        self.assertEqual(integration.resolve_prompt_target(), "s-b")

    def test_resolve_returns_none_when_no_sessions(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.bridge = FakeBridge()
        integration._available_sessions = lambda: []
        self.assertIsNone(integration.resolve_prompt_target())

    def test_repeated_prompts_land_on_the_same_session(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration.connected = True
        integration._available_sessions = lambda: [
            fake_session("s-a", running=True),
            fake_session("s-b", running=True),
        ]
        self.assertTrue(integration.prompt_active("第一条"))
        first_target = bridge.prompts[-1][0]
        # Repeated sends should never bounce to another session without the
        # user picking one explicitly: the auto‑follow is sticky.
        for _ in range(3):
            self.assertTrue(integration.prompt_active("继续"))
            self.assertEqual(bridge.prompts[-1][0], first_target)
        self.assertTrue(all(p[0] == first_target for p in bridge.prompts))

    def test_pinned_session_remains_target_after_active_is_set(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration.connected = True
        integration._available_sessions = lambda: [
            fake_session("s-pinned", running=False),
            fake_session("s-running", running=True),
        ]
        integration.select_session("s-pinned")
        # The user's pinned project must win over the first running one,
        # even if _active_session happens to be None at the time of input.
        self.assertTrue(integration.prompt_active("工作内容"))
        self.assertEqual(bridge.prompts[-1][0], "s-pinned")
        self.assertEqual(integration._active_session, "s-pinned")


# ---------------------------------------------------------------------------
# 2. bridge model_catalog + select_session_model


class BridgeModelCatalogTests(unittest.TestCase):
    def test_model_catalog_parses_canonical_entries(self):
        bridge = DshBridge(timeout=0.1)
        bridge._post = lambda path, body: {
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {
                "ok": True,
                "value": {
                    "default": {"provider": "deepseek", "model": "deepseek-chat"},
                    "routableProviders": ["deepseek"],
                    "groups": [
                        {
                            "id": "deepseek",
                            "name": "DeepSeek",
                            "models": [
                                {"id": "deepseek-chat", "name": "DeepSeek Chat"},
                                {
                                    "id": "deepseek-reasoner",
                                    "name": "DeepSeek R1",
                                    "reasoning": {
                                        "efforts": [
                                            {"id": "high", "name": "高"},
                                        ],
                                        "defaultEffort": "high",
                                    },
                                },
                            ],
                        }
                    ],
                    "failures": [],
                },
            },
        }
        catalog = bridge.model_catalog()
        self.assertEqual([c.model_id for c in catalog], ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(catalog[0].label, "DeepSeek Chat")
        self.assertEqual(catalog[0].provider, "deepseek")
        self.assertEqual(catalog[0].provider_label, "DeepSeek")
        self.assertEqual(catalog[1].reasoning_efforts[0].effort_id, "high")
        self.assertEqual(catalog[1].default_reasoning_effort, "high")

    def test_model_catalog_rejects_non_object_value(self):
        bridge = DshBridge(timeout=0.1)
        bridge._post = lambda path, body: {
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": ["m-a", "m-b"]},
        }
        with self.assertRaises(DshError):
            bridge.model_catalog()

    def test_model_catalog_skips_invalid_entries(self):
        bridge = DshBridge(timeout=0.1)
        bridge._post = lambda path, body: {
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {
                "ok": True,
                "value": {
                    "default": {"provider": "deepseek", "model": "good"},
                    "routableProviders": ["deepseek"],
                    "groups": [
                        {
                            "id": "deepseek",
                            "name": "DeepSeek",
                            "models": [
                                {},
                                {"id": "  ", "name": "Invalid"},
                                {"id": "good", "name": "Good"},
                            ],
                        }
                    ],
                    "failures": [],
                },
            },
        }
        catalog = bridge.model_catalog()
        self.assertEqual([c.model_id for c in catalog], ["good"])

    def test_select_session_model_sends_three_wire_arguments(self):
        bridge = DshBridge(timeout=0.1)
        captured = {}

        def post(path, body):
            captured["path"] = path
            captured["args"] = body["payload"]["args"]
            return {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": {"ok": True}},
            }

        bridge._post = post
        bridge.select_session_model("session-1", "deepseek-reasoner", "deepseek", "high")
        self.assertEqual(captured["path"], "/api/session/selectModel")
        self.assertEqual(
            captured["args"],
            {
                "request": {
                    "sessionId": "session-1",
                    "provider": "deepseek",
                    "model": "deepseek-reasoner",
                    "reasoningEffort": "high",
                }
            },
        )

    def test_select_session_model_empty_is_noop_and_never_calls_post(self):
        bridge = DshBridge(timeout=0.1)
        called = {"count": 0}

        def post(path, body):
            called["count"] += 1
            return {"type": "server-response", "rpcId": body["rpcId"], "result": {"ok": True}}

        bridge._post = post
        result = bridge.select_session_model("session-1", "", "")
        self.assertEqual(called["count"], 0)
        self.assertTrue(result.get("noop"))

    def test_select_session_model_propagates_errors(self):
        bridge = DshBridge(timeout=0.1)
        bridge._post = lambda path, body: {
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": False, "error": {"code": "invalid", "message": "bad model"}},
        }
        with self.assertRaises(DshError):
            bridge.select_session_model("session-1", "nope", "deepseek")


# ---------------------------------------------------------------------------
# 3. integration model picker


class IntegrationModelPickerTests(unittest.TestCase):
    def test_first_refresh_fetches_once_and_caches(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge(catalog=("m1", "m2", "m3"))
        integration.bridge = bridge
        self.assertEqual(bridge.catalog_calls, 0)
        catalog = integration.session_model_choices()
        self.assertEqual([c.model_id for c in catalog], ["m1", "m2", "m3"])
        self.assertEqual(bridge.catalog_calls, 1)
        # A second call without force_refresh reuses the cache.
        catalog = integration.session_model_choices()
        self.assertEqual(bridge.catalog_calls, 1)

    def test_force_refresh_refetches(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration.session_model_choices()
        integration.session_model_choices(force_refresh=True)
        self.assertEqual(bridge.catalog_calls, 2)

    def test_bridge_failure_keeps_cache_and_sets_error(self):
        engine = make_engine()
        integration = DshIntegration(engine)

        class BoomBridge(FakeBridge):
            def model_catalog(self):
                raise DshError("dsh rpc model/catalog: 500: gone")

        bridge = BoomBridge()
        integration.bridge = bridge
        integration.session_model_choices()
        # A second refresh must keep the prior cache alive and surface
        # the error string to the menu.
        integration._model_catalog = [ModelCatalogEntry(model_id="prior")]
        integration.session_model_choices(force_refresh=True)
        self.assertEqual([c.model_id for c in integration.session_model_choices()], ["prior"])
        self.assertIn("gone", integration._model_catalog_error or "")

    def test_select_session_model_commits_locally_and_calls_bridge(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration._active_session = "session-1"
        integration.mode = "agent"
        integration._agent_session_id = "session-1"
        self.assertTrue(
            integration.select_session_model(
                "session-1", "deepseek-reasoner", "deepseek"
            )
        )
        self.assertEqual(
            bridge.model_selections,
            [("session-1", "deepseek", "deepseek-reasoner", None)],
        )
        self.assertEqual(
            integration.current_session_model("session-1"),
            ("deepseek", "deepseek-reasoner", ""),
        )

    def test_select_session_model_empty_string_is_noop(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration._active_session = "session-1"
        integration.mode = "agent"
        integration._agent_session_id = "session-1"
        self.assertTrue(integration.select_session_model("session-1", "", ""))
        self.assertEqual(bridge.model_selections, [])

    def test_select_session_model_without_active_session_is_rejected(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        # No active session: user-facing failure.
        self.assertFalse(integration.select_session_model(None, "x", "deepseek"))

    def test_select_session_model_bridge_failure_bubbles_but_keeps_cache(self):
        engine = make_engine()
        integration = DshIntegration(engine)

        class FailBridge(FakeBridge):
            def select_session_model(self, sid, mid, provider, effort=None):
                raise DshError("dsh rpc session/selectModel: 503: nope")

        bridge = FailBridge()
        integration.bridge = bridge
        integration._active_session = "session-1"
        self.assertFalse(integration.select_session_model("session-1", "x", "deepseek"))
        self.assertEqual(integration.current_session_model("session-1"), ("", "", ""))

    def test_link_mode_cannot_change_work_session_model(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration.connected = True
        integration._active_session = "work-session"
        self.assertFalse(
            integration.select_session_model("work-session", "deepseek-chat", "deepseek")
        )
        self.assertEqual(bridge.model_selections, [])

    def test_agent_model_changes_only_shadow_session(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        bridge = FakeBridge()
        integration.bridge = bridge
        integration.mode = "agent"
        integration._agent_session_id = "shadow-session"
        integration._active_session = "work-session"
        integration._session_models["work-session"] = (
            "deepseek", "deepseek-reasoner", ""
        )
        self.assertTrue(
            integration.select_session_model("shadow-session", "deepseek-chat", "deepseek")
        )
        self.assertEqual(
            bridge.model_selections,
            [("shadow-session", "deepseek", "deepseek-chat", None)],
        )
        self.assertEqual(
            integration.current_session_model("work-session"),
            ("deepseek", "deepseek-reasoner", ""),
        )

    def test_apply_sessions_caches_current_session_model_from_projection(self):
        engine = make_engine()
        integration = DshIntegration(engine)
        integration.bridge = FakeBridge()
        integration._apply_sessions(
            [
                fake_session(
                    "s-a",
                    running=True,
                    agent_preset="zcode",
                    model_provider="deepseek",
                    model_id="deepseek-reasoner",
                    reasoning_effort="high",
                )
            ]
        )
        self.assertEqual(
            integration.current_session_model("s-a"),
            ("deepseek", "deepseek-reasoner", "high"),
        )
        self.assertEqual(integration._harness_raw, "zcode")


# ---------------------------------------------------------------------------
# 4. qt window menu (independent of 摘要模型)


if HAS_QT:

    class QtModelMenuTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls) -> None:
            cls.app = QApplication.instance() or QApplication([])

        def _make(self):
            integration = DshIntegration(make_engine())
            bridge = FakeBridge(catalog=("deepseek-chat", "deepseek-reasoner"))
            integration.bridge = bridge
            integration._active_session = "session-1"
            integration._session_models["session-1"] = (
                "deepseek",
                "deepseek-chat",
                "",
            )
            return integration, bridge

        def test_model_menu_lists_catalog_entries_and_marks_current(self):
            from PySide6.QtWidgets import QMenu

            from mimi_pet.qt_window import MimiWindow, QtRenderer

            integration, _ = self._make()
            window = MimiWindow.__new__(MimiWindow)
            window.dsh = integration
            from PySide6.QtWidgets import QWidget

            QWidget.__init__(window)
            menu = QMenu(window)
            integration.mode = "agent"
            integration._agent_session_id = "session-1"
            window._build_pet_model_menu(menu)
            model_menu = next(
                child
                for child in menu.findChildren(QMenu)
                if child.parent() is menu and child.title() == "桌宠模型"
            )
            labels = [action.text() for action in model_menu.actions()]
            self.assertIn("DeepSeek · deepseek-chat", labels)
            self.assertIn("DeepSeek · deepseek-reasoner", labels)
            checked = [action for action in model_menu.actions() if action.isChecked()]
            self.assertEqual([a.text() for a in checked], ["DeepSeek · deepseek-chat"])

        def test_model_menu_is_separate_from_summary_menu(self):
            """The 摘要模型 and 桌宠模型 submenus must coexist on the
            Harness menu and never share entries."""
            from PySide6.QtWidgets import QMenu

            from mimi_pet.qt_window import MimiWindow

            integration, _ = self._make()
            window = MimiWindow.__new__(MimiWindow)
            window.dsh = integration
            from PySide6.QtWidgets import QWidget

            QWidget.__init__(window)
            dsh_menu = QMenu(window)
            integration.mode = "agent"
            integration._agent_session_id = "session-1"
            window._build_pet_model_menu(dsh_menu)
            cot_menu = dsh_menu.addMenu("摘要模型")
            window._build_cot_menu(cot_menu)
            titles = [m.title() for m in dsh_menu.findChildren(QMenu) if m.parent() is dsh_menu]
            self.assertIn("桌宠模型", titles)
            self.assertIn("摘要模型", titles)


if __name__ == "__main__":
    unittest.main()