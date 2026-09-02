"""Harness head-top UI: one white input box (+ pending question cards).

All DSH information — assistant replies, summaries, tool activity, done
notices — surfaces as transient speech bubbles in the BubbleLayer. This
window is interaction-only: a Chinese-IME-safe quick input pinned above
Mimi's head, growing orange-bordered question cards while a DSH question
waits for the user's answer.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, Qt, Signal, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .dsh_bridge import dbg

PANEL_QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei"; }
QFrame#inputCard {
    background: rgba(255, 255, 255, 248);
    border: 1px solid rgba(150, 162, 186, 130);
    border-radius: 16px;
}
QLineEdit {
    background: transparent;
    border: none;
    color: #2b3245;
    font-size: 12px;
    padding: 3px 2px 3px 12px;
    selection-background-color: rgba(91, 141, 239, 160);
}
QLabel#modeBadge {
    background: rgba(95, 134, 237, 34);
    border: 1px solid rgba(95, 134, 237, 105);
    border-radius: 8px;
    color: #4d6fc9;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
}
QLabel#modeBadge[mode="agent"] {
    background: rgba(95, 211, 176, 38);
    border-color: rgba(55, 170, 137, 120);
    color: #2f9478;
}
QPushButton#sendBtn {
    background: #5f86ed; border: none; border-radius: 12px;
    color: white; font-weight: 600; font-size: 12px; padding: 4px 12px;
}
QPushButton#sendBtn:hover { background: #7198ff; }
QLabel#qTitle { color: #2b3245; font-weight: bold; font-size: 12px; }
QLabel#qDetail { color: #5a6478; font-size: 11px; }
QPushButton#questionBtn { background: #5f86ed; border: none; color: white; }
QPushButton {
    background: rgba(78, 94, 142, 78);
    border: 1px solid rgba(132, 154, 218, 45);
    border-radius: 9px;
    color: #dce6ff;
    font-size: 12px;
    padding: 3px 10px;
}
QPushButton:hover { background: rgba(101, 128, 205, 125); }
"""

INPUT_W = 250
MAX_QUESTIONS = 3


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


class _QuickInput(QLineEdit):
    """Single-line quick message input.

    QLineEdit — not QPlainTextEdit — because QPlainTextEdit's Windows IME
    (Chinese pinyin) is broken in Qt/PySide6: composition gets forced to
    English and typing can stall. QLineEdit handles the IME internally and
    only emits returnPressed on a genuine Enter (composition keys are never
    hijacked, so Chinese works and Enter never sends mid-composition).
    Esc clears the text.
    """

    submitted = Signal(str)
    collapse_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(30)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        self.returnPressed.connect(self._do_send)

    def _do_send(self) -> None:
        text = self.text().strip()
        if text:
            self.submitted.emit(text)
            self.clear()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_Escape:
            self.collapse_requested.emit()
            return
        super().keyPressEvent(event)


class QuestionCard(QWidget):
    """One pending DSH question: option buttons or a free-text answer."""

    answered = Signal(object, list, str)  # question, selected labels, custom

    def __init__(self, question) -> None:
        super().__init__()
        self._question = question
        self.setObjectName("questionCard")
        self.setStyleSheet(
            "#questionCard { background: rgba(255, 255, 255, 248);"
            " border: 1px solid rgba(255, 159, 67, 190); border-radius: 12px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(4)

        title = question.header or question.question
        if question.header and question.question and question.header not in question.question:
            title = f"{question.header}：{question.question}"
        label = QLabel(_clip(title, 44))
        label.setObjectName("qTitle")
        label.setWordWrap(True)
        label.setToolTip(title)
        layout.addWidget(label)
        if question.detail:
            detail = QLabel(_clip(question.detail, 44))
            detail.setObjectName("qDetail")
            detail.setWordWrap(True)
            detail.setToolTip(question.detail)
            layout.addWidget(detail)

        if question.options:
            row = QHBoxLayout()
            row.setSpacing(6)
            for option in question.options:
                opt_label = str(option.get("label", option))
                btn = QPushButton(_clip(opt_label, 10))
                if question.multi_select:
                    btn.setCheckable(True)
                    btn.clicked.connect(
                        lambda checked=False, b=btn, l=opt_label: self._toggle(b, checked, l)
                    )
                else:
                    btn.clicked.connect(lambda checked=False, l=opt_label: self._submit([l]))
                row.addWidget(btn)
            if question.multi_select:
                confirm = QPushButton("确认")
                confirm.setObjectName("questionBtn")
                confirm.clicked.connect(lambda: self._submit(list(self._selected)))
                row.addWidget(confirm)
            layout.addLayout(row)
        else:
            custom = QLineEdit()
            custom.setPlaceholderText("输入回答，回车发送…")
            custom.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            custom.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
            custom.setInputMethodHints(Qt.InputMethodHint.ImhNone)
            custom.returnPressed.connect(lambda: self._submit([], custom.text().strip()))
            layout.addWidget(custom)

        self._selected: list[str] = []

    def _toggle(self, btn: QPushButton, checked: bool, label: str) -> None:
        if checked and label not in self._selected:
            self._selected.append(label)
        elif not checked and label in self._selected:
            self._selected.remove(label)

    def _submit(self, selected: list[str], custom: str = "") -> None:
        self.answered.emit(self._question, selected, custom)
        # Answer delivery is confirmed by the integration; the card itself
        # always closes so the input row is reachable again.
        self.deleteLater()


class DshPanel(QWidget):
    """The single white input box above the pet; questions stack above it."""

    send_requested = Signal(str)
    answer_requested = Signal(object, list, str)  # question, selected labels, custom
    bubble_requested = Signal(str, str)  # transient message bubbles (text, kind)

    MAX_ROWS = MAX_QUESTIONS  # compat constant for old scripts

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Keep Windows IME working even though the window is frameless.
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(PANEL_QSS)
        self.setMouseTracking(True)
        self._anchor: tuple[float, float] | None = None
        self._state = None

        # --- question stack (only while DSH waits for the user) ---
        self._questions: list[QuestionCard] = []
        self._question_host = QWidget()
        self._question_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._q_layout = QVBoxLayout(self._question_host)
        self._q_layout.setContentsMargins(0, 0, 0, 0)
        self._q_layout.setSpacing(4)

        # --- the white input card ---
        self._card = QFrame()
        self._card.setObjectName("inputCard")
        self._card.setFixedHeight(40)
        row = QHBoxLayout(self._card)
        row.setContentsMargins(4, 4, 5, 4)
        row.setSpacing(4)
        self.mode_badge = QLabel("工作模式")
        self.mode_badge.setObjectName("modeBadge")
        self.mode_badge.setProperty("mode", "link")
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_badge.setToolTip("当前：工作模式")
        self.quick_input = _QuickInput()
        self.quick_input.setPlaceholderText("输入消息…")
        send_btn = QPushButton("发送")
        send_btn.setObjectName("sendBtn")
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setFixedHeight(30)
        send_btn.clicked.connect(lambda: self._quick_send(self.quick_input.text()))
        row.addWidget(self.mode_badge)
        row.addWidget(self.quick_input, 1)
        row.addWidget(send_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addWidget(self._question_host)
        outer.addWidget(self._card)

        self.quick_input.submitted.connect(self._quick_send)
        self.quick_input.collapse_requested.connect(self.quick_input.clear)
        self.setFixedWidth(INPUT_W)
        self._relayout()

    # ------------------------------------------------------------------ layout

    def _relayout(self) -> None:
        self._question_host.setVisible(bool(self._questions))
        self.setFixedWidth(INPUT_W)
        self.adjustSize()
        if self._anchor is not None and self.isVisible():
            self.position_near(*self._anchor)

    # ------------------------------------------------------------------ modes

    def show_box(self) -> None:
        """Reveal the input above the head without stealing focus."""
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if not self.isVisible():
            self.show()
            self.raise_()

    show_capsule = show_box  # compat alias

    def show_expanded(self) -> None:
        """Compat alias: focus the input for typing."""
        self.show_panel()

    def show_panel(self) -> None:
        """Reveal and focus the input (user-initiated: bubble/menu click)."""
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self._focus_quick_input)

    def _focus_quick_input(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            # Follows a direct user click, so foreground activation is
            # permitted and the selected Chinese IME can attach.
            try:
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except (AttributeError, OSError):
                pass
        self.quick_input.setFocus(Qt.FocusReason.OtherFocusReason)
        QGuiApplication.inputMethod().update(
            Qt.InputMethodQuery.ImEnabled
            | Qt.InputMethodQuery.ImHints
            | Qt.InputMethodQuery.ImCursorRectangle
        )

    def _quick_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.quick_input.clear()
        self.send_requested.emit(text)

    # ------------------------------------------------------------------ position

    def position_near(self, root_x: float, head_y: float) -> None:
        """Pin the input box just above the pet's head (centered on it)."""
        self._anchor = (root_x, head_y)
        w, h = self.width(), self.height()
        x = int(round(root_x - w / 2.0))
        y = int(round(head_y - h - 4.0))
        screen = QGuiApplication.screenAt(QPoint(int(root_x), int(head_y)))
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - w))
            y = max(geo.top(), y)
        # Only move when the position actually changed: repeated move() calls
        # would interrupt the IME composition while typing Chinese.
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    # ------------------------------------------------------------------ status

    def set_status(self, text: str) -> None:
        self.setToolTip((text or "").strip())

    def set_activity(self, state) -> None:
        """Show the current mode explicitly and keep the input hint contextual."""
        self._state = state
        mode = getattr(state, "mode", "link") or "link"
        mode_name = getattr(state, "mode_display_name", "") or (
            "桌宠模式" if mode == "agent" else "工作模式"
        )
        self.mode_badge.setText(mode_name)
        self.mode_badge.setProperty("mode", mode)
        self.mode_badge.setToolTip(f"当前：{mode_name}")
        self.mode_badge.style().unpolish(self.mode_badge)
        self.mode_badge.style().polish(self.mode_badge)
        harness = getattr(state, "harness_display_name", "") or ""
        if getattr(state, "is_connected", False):
            if mode == "agent":
                self.quick_input.setPlaceholderText("和 Mimi 说点什么…")
            else:
                self.quick_input.setPlaceholderText(f"跟 {harness or 'DSH'} 工作…")
        else:
            self.quick_input.setPlaceholderText("未连接 DSH")
        self.set_status(getattr(state, "full_activity", ""))

    # ------------------------------------------------------------ sink protocol

    def append_message(self, role: str, text: str) -> None:
        """Info surfaces as bubbles; the input box shows no history."""

    def set_row(self, tag: str, role: str, text: str) -> None:
        """Info surfaces as bubbles; the input box shows no history."""

    def upsert_progress(self, text: str) -> None:
        """Progress is carried by the pet's work animation."""

    def set_project_provider(self, choices_fn, select_fn, current_fn) -> None:
        """Project picking moved to the pet's right-click Harness menu."""

    def append_question(self, question) -> None:
        """Stack a pending question card above the input."""
        card = QuestionCard(question)
        card.answered.connect(self._on_question_answered)
        self._questions.append(card)
        self._q_layout.addWidget(card)
        while len(self._questions) > MAX_QUESTIONS:
            self._drop_question(self._questions[0])
        self._relayout()
        self.show_box()
        self.raise_()

    def replace_questions(self, questions) -> None:
        """Replace cards after a project switch or a remote resolution."""
        for card in list(self._questions):
            self._drop_question(card)
        for question in questions:
            self.append_question(question)

    def _on_question_answered(self, question, selected: list[str], custom: str) -> None:
        self._answer(question, selected, custom)
        for card in self._questions:
            if card._question is question:  # noqa: SLF001 (same-module peer)
                self._drop_question(card)
                return

    def _drop_question(self, card: QuestionCard) -> None:
        if card in self._questions:
            self._questions.remove(card)
            self._q_layout.removeWidget(card)
            card.deleteLater()
            self._relayout()

    def _answer(self, question, selected: list[str], custom: str = "") -> None:
        self.answer_requested.emit(question, selected, custom)

    def show_bubble(self, text: str, kind: str = "assistant") -> None:
        """Sink hook: forward a transient DSH bubble to the bubble layer."""
        dbg(f"panel.show_bubble emit kind={kind} len={len(text or '')}")
        self.bubble_requested.emit(text, kind)

    def clear(self) -> None:
        for card in list(self._questions):
            self._drop_question(card)
