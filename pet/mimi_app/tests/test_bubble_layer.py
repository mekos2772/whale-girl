"""Transient DSH speech bubbles: stacking, expiry, click and emission."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:
    HAS_QT = False

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.dsh_bridge import DshEvent  # noqa: E402
from mimi_pet.dsh_integration import DshIntegration  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402


class Recorder:
    def __init__(self) -> None:
        self.calls: list = []

    def show_bubble(self, text: str, kind: str = "assistant") -> None:
        self.calls.append((text, kind))


@unittest.skipUnless(HAS_QT, "PySide6 is not installed; bubble test skipped")
class BubbleLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _layer(self):
        from mimi_pet.bubble_layer import BubbleLayer

        layer = BubbleLayer()
        self.addCleanup(layer.hide)
        return layer

    def test_stack_is_capped_at_three(self) -> None:
        layer = self._layer()
        for i in range(5):
            layer.add_bubble(f"消息 {i}", "assistant")
        self.assertEqual(len(layer.bubbles()), 3)
        # newest survive, oldest dropped
        texts = [chip._full for chip in layer.bubbles()]
        self.assertEqual(texts, ["消息 2", "消息 3", "消息 4"])

    def test_expiry_removes_the_bubble(self) -> None:
        layer = self._layer()
        layer.add_bubble("会消散", "assistant", lifetime_s=0.05)
        self.assertEqual(len(layer.bubbles()), 1)
        QTest.qWait(200)
        self.assertEqual(len(layer.bubbles()), 0)
        self.assertFalse(layer.isVisible())

    def test_click_opens_the_card(self) -> None:
        layer = self._layer()
        fired = []
        layer.message_clicked.connect(lambda: fired.append(True))
        layer.add_bubble("点我", "question", lifetime_s=60.0)
        QTest.mouseClick(layer.bubbles()[0], Qt_MsButton())
        self.assertTrue(fired)
        QTest.qWait(50)
        self.assertEqual(len(layer.bubbles()), 0)  # clicked bubbles retire


def Qt_MsButton():
    from PySide6.QtCore import Qt

    return Qt.MouseButton.LeftButton


@unittest.skipUnless(HAS_QT, "PySide6 is not installed; panel bubble test skipped")
class PanelBubbleSinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_panel_show_bubble_forwards_to_signal(self) -> None:
        from mimi_pet.dsh_panel import DshPanel

        panel = DshPanel()
        self.addCleanup(panel.close)
        self.addCleanup(panel.hide)
        got: list = []
        panel.bubble_requested.connect(lambda text, kind: got.append((text, kind)))
        panel.show_bubble("你好", "assistant")
        self.assertEqual(got, [("你好", "assistant")])


class IntegrationBubbleTests(unittest.TestCase):
    """DSH events surface as bubbles (with throttling), independent of Qt."""

    @classmethod
    def setUpClass(cls) -> None:
        config = load_config()
        cls.engine = PetEngine(ActionLibrary(Path(config["asset_manifest"])), config)

    def _integration(self) -> tuple[DshIntegration, Recorder]:
        integration = DshIntegration(self.__class__.engine)
        recorder = Recorder()
        integration.sink = recorder
        integration._last_bubble_at = -1e9
        return integration, recorder

    def _session_event(self, integration: DshIntegration, event: dict) -> None:
        integration._handle_event(
            DshEvent(
                method="session/event",
                rpc_id="",
                payload={"sessionId": "s1", "event": event},
            )
        )

    def test_final_assistant_reply_bubbles(self) -> None:
        integration, recorder = self._integration()
        self._session_event(
            integration,
            {
                "type": "assistant/chunk",
                "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "修好了"}},
            },
        )
        self._session_event(integration, {"type": "assistant/message", "data": {}})
        self.assertIn(("修好了", "assistant"), recorder.calls)

    def test_throttling_drops_rapid_infos_but_not_questions(self) -> None:
        integration, recorder = self._integration()
        integration._bubble("第一条", kind="info")
        integration._bubble("第二条", kind="info")  # <1.2s -> dropped
        self.assertEqual([c for c in recorder.calls if c[1] == "info"], [("第一条", "info")])
        integration._bubble("问你：重启吗？", kind="question")  # important -> shown
        self.assertIn(("问你：重启吗？", "question"), recorder.calls)

    def test_summary_result_bubbles(self) -> None:
        integration, recorder = self._integration()
        # Summaries only bubble after real tool work in the turn.
        self._session_event(
            integration,
            {"type": "tool/call", "data": {"name": "pwsh", "arguments": "{}"}},
        )
        integration._last_bubble_at = -1e9  # bypass the throttle in tests
        integration._handle_event(
            DshEvent(
                method="cot/result",
                rpc_id="",
                payload={"tag": "reason_1_1", "text": "定位到 patch 层问题"},
            )
        )
        self.assertIn(("总结：定位到 patch 层问题", "summary"), recorder.calls)


if __name__ == "__main__":
    unittest.main()
