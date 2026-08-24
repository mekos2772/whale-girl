"""Offscreen checks for the input-box Harness panel (bubbles carry info)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from mimi_pet.dsh_integration import DshQuestion
    from mimi_pet.dsh_panel import INPUT_W, MAX_QUESTIONS, DshPanel, QuestionCard

    HAS_QT = True
except ImportError:
    HAS_QT = False


def _question(qid: str = "q1") -> DshQuestion:
    return DshQuestion(
        rpc_id="rpc-1",
        session_id="s-1",
        id=qid,
        question="是否重启 DSH？",
        options=[{"label": "是"}, {"label": "稍后"}],
    )


@unittest.skipUnless(HAS_QT, "PySide6 is not installed; panel flow test skipped")
class DshPanelFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self) -> DshPanel:
        panel = DshPanel()
        self.addCleanup(panel.close)
        self.addCleanup(panel.hide)
        return panel

    def test_quick_input_submits_and_clears(self) -> None:
        panel = self._panel()
        sent: list[str] = []
        panel.send_requested.connect(sent.append)
        panel.quick_input.setText("你好呀")
        panel.quick_input._do_send()
        self.assertEqual(sent, ["你好呀"])
        self.assertEqual(panel.quick_input.text(), "")

    def test_show_bubble_forwards_to_signal(self) -> None:
        panel = self._panel()
        got: list[tuple[str, str]] = []
        panel.bubble_requested.connect(lambda text, kind: got.append((text, kind)))
        panel.show_bubble("收到喵～", "assistant")
        self.assertEqual(got, [("收到喵～", "assistant")])

    def test_information_sinks_are_noops(self) -> None:
        panel = self._panel()
        panel.set_row("t", "tool", "Pwsh · main.py")
        panel.append_message("info", "提示")
        panel.upsert_progress("第 2 步")
        panel.set_status("工作中…")
        self.assertEqual(panel.quick_input.text(), "")

    def test_question_card_answers_and_closes(self) -> None:
        panel = self._panel()
        got: list = []
        panel.answer_requested.connect(lambda q, sel, custom: got.append((q, sel, custom)))
        panel.append_question(_question())
        self.assertEqual(len(panel._questions), 1)
        panel._questions[0]._submit(["是"])
        self.assertEqual(got, [(_question(), ["是"], "")])
        self.assertEqual(panel._questions, [])

    def test_question_stack_capped_and_cleared(self) -> None:
        panel = self._panel()
        for i in range(MAX_QUESTIONS + 2):
            panel.append_question(_question(f"q{i}"))
        self.assertEqual(len(panel._questions), MAX_QUESTIONS)
        panel.clear()
        self.assertEqual(panel._questions, [])

    def test_activity_sets_placeholder(self) -> None:
        panel = self._panel()

        class State:
            is_connected = True
            harness_display_name = "Mimi"

        panel.set_activity(State())
        self.assertIn("Mimi", panel.quick_input.placeholderText())

        class Offline:
            is_connected = False
            harness_display_name = ""

        panel.set_activity(Offline())
        self.assertIn("未连接", panel.quick_input.placeholderText())

    def test_input_box_fixed_width(self) -> None:
        panel = self._panel()
        panel.position_near(500.0, 300.0)
        panel.show_box()
        self.app.processEvents()
        self.assertEqual(panel.width(), INPUT_W)
        self.assertTrue(
            panel.quick_input.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        )

    def test_question_card_renders_options(self) -> None:
        from PySide6.QtWidgets import QPushButton

        panel = self._panel()
        panel.append_question(_question())
        card = panel._questions[0]
        self.assertIsInstance(card, QuestionCard)
        labels = [b.text() for b in card.findChildren(QPushButton)]
        self.assertIn("是", labels)
        self.assertIn("稍后", labels)


if __name__ == "__main__":
    unittest.main()
