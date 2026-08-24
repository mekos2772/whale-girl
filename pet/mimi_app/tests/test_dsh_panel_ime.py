"""Windows/Qt input-method contract for the Harness quick input."""

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
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtWidgets import QApplication

    from mimi_pet.dsh_panel import DshPanel

    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "PySide6 is not installed; IME test skipped")
class DshPanelImeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_expanded_window_accepts_chinese_ime_commit(self) -> None:
        panel = DshPanel()
        panel.position_near(500.0, 300.0)
        panel.show_expanded()
        self.app.processEvents()

        self.assertEqual(panel.windowType(), Qt.WindowType.Window)
        self.assertTrue(panel.quick_input.testAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled))

        commit = QInputMethodEvent()
        commit.setCommitString("中文输入")
        QApplication.sendEvent(panel.quick_input, commit)
        self.assertEqual(panel.quick_input.text(), "中文输入")
        panel.close()


if __name__ == "__main__":
    unittest.main()
