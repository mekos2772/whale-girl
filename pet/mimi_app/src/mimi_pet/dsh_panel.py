"""Harness head-top UI: activity capsule + quick input.

Default state is a small pill ("activity capsule") pinned just above the pet's
head showing the *concrete current activity summary* — e.g. "检查 DSH 结构…"
or "修改 action/action.json…" — with an optional harness badge, a live status
dot and a chevron hint. Clicking the pill expands a lightweight quick-input
below it as the entry point to keep talking to the running Harness (reusing
the same send path as the old panel input). A narrower choice: the button
with "☰" reveals the full message list (previous DSH panel body).

The capsule is a separate top-level Tool window, so clicking it never reaches
the pet window and can never be mistaken for a drag start.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .activity_text import compress_summary_to

PANEL_QSS = """
QListWidget {
    background: rgba(255, 255, 255, 240);
    border: none;
    color: #1e1e1e;
    font-family: "Microsoft YaHei";
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 10px;
}
QListWidget::item { padding: 0px 2px; }
QLabel#headerStatus {
    color: #8a8a9c;
    font-family: "Microsoft YaHei";
    font-size: 11px;
}
QLabel#harnessBadge {
    color: #7d84a6;
    font-family: "Microsoft YaHei";
    font-size: 11px;
}
QLabel#summaryText {
    color: #1c1c26;
    font-family: "Microsoft YaHei";
    font-size: 13px;
    font-weight: 500;
}
QLabel#chevronHint {
    color: #b6b6c8;
    font-family: "Microsoft YaHei";
    font-size: 11px;
}
QPlainTextEdit {
    background: rgba(250, 250, 252, 255);
    border: 1px solid rgba(185, 185, 202, 230);
    border-radius: 10px;
    color: #1e1e1e;
    font-family: "Microsoft YaHei";
    font-size: 12px;
    padding: 5px 9px;
}
QPlainTextEdit:focus { border-color: rgba(91, 141, 239, 220); background: white; }
QPushButton {
    background: rgba(255, 255, 255, 165);
    border: none;
    border-radius: 9px;
    color: #44445a;
    font-family: "Microsoft YaHei";
    font-size: 12px;
    padding: 3px 10px;
}
QPushButton:hover { background: rgba(255, 255, 255, 215); }
QPushButton#sendBtn {
    background: rgba(91, 141, 239, 210);
    color: white;
}
QPushButton#sendBtn:hover { background: rgba(110, 160, 250, 230); }
QPushButton#iconBtn { padding: 3px 7px; color: #9a9aae; }
QPushButton#iconBtn:hover { background: rgba(220, 80, 80, 170); color: white; }
QPushButton#questionBtn {
    background: rgba(91, 141, 239, 210);
    border-radius: 8px;
    color: white;
    padding: 3px 10px;
}
QPushButton#questionBtn:hover { background: rgba(110, 160, 250, 230); }
QPushButton#projectBtn {
    background: rgba(120, 130, 200, 36);
    border: none;
    border-radius: 8px;
    color: #6a6f96;
    font-size: 10px;
    padding: 1px 8px;
}
QPushButton#projectBtn:hover { background: rgba(120, 130, 200, 95); color: #3f4470; }
QMenu {
    background: #ffffff;
    border: 1px solid rgba(0, 0, 0, 26);
    border-radius: 10px;
    padding: 4px;
    font-family: "Microsoft YaHei";
    font-size: 11px;
}
QMenu::item { padding: 4px 14px; border-radius: 6px; }
QMenu::item:selected { background: rgba(91, 141, 239, 70); }
QMenu::item:disabled { color: #b6b6c8; }
QLabel#qTitle { color: #1e1e1e; font-weight: bold; font-size: 12px; }
QLabel#qDetail { color: #66667c; font-size: 11px; }
"""

ROLE_LABELS = {
    "user": "你",
    "assistant": "Mimi",
    "tool": "工具",
    "progress": "进度",
    "summary": "总结",
    "question": "询问",
    "info": "提示",
}

# Official tool icons (lucide family, same as the DSH web UI).
TOOL_ICON_FILES = {
    "Search": "search",
    "Read": "book",
    "Bash": "terminal",
    "Pwsh": "terminal",
    "Write": "file-pen",
    "Edit": "pencil",
    "Code": "code",
    "Tool call": "terminal",
}
TOOL_ICONS_DIR = Path(__file__).resolve().parents[3] / "assets" / "dsh_tool_icons"

STATUS_COLORS = {
    "connected": QColor(0x3E, 0xCF, 0x8E),
    "thinking": QColor(0xF5, 0xA6, 0x23),
    "executing": QColor(0x4A, 0x9B, 0xFF),
    "tool": QColor(0x5B, 0x8D, 0xEF),
    "waiting": QColor(0xFF, 0x9F, 0x43),
    "done": QColor(0x4C, 0xD9, 0x64),
    "fail": QColor(0xF2, 0x60, 0x5A),
    "disconnected": QColor(0xB0, 0xB0, 0xBD),
}
PULSE_STATUS = {"thinking", "executing", "tool", "waiting"}
WAIT_ACCENT = QColor(0xFF, 0x9F, 0x43, 200)

MAX_CHARS = 34
LISTROW_MAX = 34
RECENT_KEEP = 5
PANEL_W, PANEL_H = 360, 232
# The head-top capsule: FIXED width, wraps instead of ellipsizing so more text
# is visible; height grows with line count up to MAX_CAPSULE_LINES.
CAPSULE_H = 30
CAPSULE_W = 280
MAX_CAPSULE_LINES = 3
INPUT_H = 44
_EDGE = 10  # exclusive dot+chevron+padding budget in the pill


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


class StatusDot(QWidget):
    """Small status circle; gently pulses for thinking/executing/tool/waiting."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(12, 12)
        self._color = STATUS_COLORS["disconnected"]
        self._pulsing = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def set_pulsing(self, pulsing: bool) -> None:
        if pulsing == self._pulsing:
            if pulsing:
                self.update()
            return
        self._pulsing = pulsing
        if pulsing:
            self._phase = 0.0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self) -> None:
        self._phase += 0.22
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        base = 4.2
        if self._pulsing:
            ripple = 3.2 * abs(math.sin(self._phase))
            glow = QColor(self._color)
            glow.setAlpha(int(85 + 90 * abs(math.sin(self._phase))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, base + ripple, base + ripple)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(center, base, base)
        # small white highlight for a friendly look
        painter.setBrush(QColor(255, 255, 255, 120))
        painter.drawEllipse(QPointF(center.x() - 1.4, center.y() - 1.6), 1.1, 1.1)
        painter.end()


class _Pill(QWidget):
    """Clickable rounded capsule background with optional waiting accent."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accent: QColor | None = None

    def set_accent(self, color: QColor | None) -> None:
        if color != self._accent:
            self._accent = color
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = self.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(255, 255, 255, 238))
        pen = Qt.PenStyle.NoPen
        if self._accent is not None:
            painter.setPen(self._accent)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            painter.setPen(pen)
        # subtle bottom shadow line (kept inside the pill, never clipped)
        painter.setPen(QColor(0, 0, 0, 22))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()


class _QuickInput(QPlainTextEdit):
    """Enter sends, Shift+Enter inserts a newline, Esc collapses."""

    submitted = Signal(str)
    collapse_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(180)
        self.setFixedHeight(30)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

    def auto_grow(self) -> int:
        """Let the quick input grow up to ~4 lines (Shift+Enter newline)."""
        line_h = max(1, self.fontMetrics().height())
        blocks = max(1, self.document().blockCount())
        height = max(28, min(blocks * line_h + 12, 84))
        if height != self.height():
            self.setFixedHeight(height)
        return height

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.collapse_requested.emit()
            return
        super().keyPressEvent(event)


class QuestionRow(QWidget):
    """One pending user question rendered with option buttons (or free text)."""

    def __init__(self, question, emit) -> None:
        super().__init__()
        self._question = question
        self._emit = emit
        self._selected: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        title = question.header or question.question
        if question.header and question.question and question.header not in question.question:
            title = f"{question.header}：{question.question}"
        label = QLabel(_clip(title, 40))
        label.setObjectName("qTitle")
        label.setToolTip(title)
        layout.addWidget(label)
        if question.detail:
            detail = QLabel(_clip(question.detail, 40))
            detail.setObjectName("qDetail")
            detail.setToolTip(question.detail)
            layout.addWidget(detail)

        if question.options:
            row = QHBoxLayout()
            row.setSpacing(6)
            for option in question.options:
                opt_label = str(option.get("label", option))
                btn = QPushButton(_clip(opt_label, 10))
                btn.setCheckable(question.multi_select)
                if question.multi_select:
                    btn.clicked.connect(
                        lambda checked=False, b=btn, l=opt_label: self._toggle(b, checked, l)
                    )
                else:
                    btn.clicked.connect(
                        lambda checked=False, l=opt_label: self._answer([l])
                    )
                row.addWidget(btn)
            if question.multi_select:
                confirm = QPushButton("确认")
                confirm.setObjectName("questionBtn")
                confirm.clicked.connect(self._confirm)
                row.addWidget(confirm)
            layout.addLayout(row)
        else:
            custom = QLineEdit()
            custom.setPlaceholderText("输入回答，回车发送…")
            custom.returnPressed.connect(
                lambda: self._emit(self._question, [], custom.text().strip())
            )
            layout.addWidget(custom)

    def _toggle(self, btn: QPushButton, checked: bool, label: str) -> None:
        if checked and label not in self._selected:
            self._selected.append(label)
        elif not checked and label in self._selected:
            self._selected.remove(label)

    def _confirm(self) -> None:
        self._answer(self._selected)

    def _answer(self, selected: list[str]) -> None:
        self._emit(self._question, selected)


class DshPanel(QWidget):
    """Activity capsule that expands into a quick input (plus full list)."""

    send_requested = Signal(str)
    answer_requested = Signal(object, list, str)  # question, selected labels, custom

    MAX_ROWS = 5
    KEEP_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(PANEL_QSS)
        self.setMouseTracking(True)
        self._anchor: tuple[float, float] | None = None
        self._mode = "capsule"  # capsule | expanded | panel
        self._state = None
        self._capsule_w = CAPSULE_W
        self._filter_app: QApplication | None = None

        # --- capsule (default) ---
        self._dot = StatusDot()
        self._harness_label = QLabel("")
        self._harness_label.setObjectName("harnessBadge")
        self._summary_label = QLabel("DSH 已连接")
        self._summary_label.setObjectName("summaryText")
        self._summary_label.setWordWrap(True)
        self._project_btn = QPushButton("项目 ▾")
        self._project_btn.setObjectName("projectBtn")
        self._project_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._project_btn.setFixedHeight(20)
        self._project_btn.clicked.connect(self._open_project_menu)
        self._project_btn.hide()
        self._chevron = QLabel("▾")
        self._chevron.setObjectName("chevronHint")
        self._pill = _Pill()
        self._pill.setFixedHeight(CAPSULE_H)
        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(10, 2, 8, 2)
        pill_layout.setSpacing(6)
        pill_layout.addWidget(self._dot)
        pill_layout.addWidget(self._harness_label)
        pill_layout.addWidget(self._summary_label, 1)
        pill_layout.addWidget(self._project_btn)
        pill_layout.addWidget(self._chevron)
        self._pill.clicked.connect(self._on_pill_clicked)
        # Project picker wiring (set by qt_app): (choices_fn, select_fn, current_fn).
        self._choices_fn: None = None
        self._select_fn: None = None
        self._current_fn: None = None

        # --- quick input (expanded) ---
        self.quick_input = _QuickInput()
        self.quick_input.setPlaceholderText("输入消息…")
        send_btn = QPushButton("发送")
        send_btn.setObjectName("sendBtn")
        send_btn.setFixedWidth(52)
        send_btn.clicked.connect(lambda: self._quick_send(self.quick_input.toPlainText()))
        toggle_btn = QPushButton("☰")
        toggle_btn.setObjectName("iconBtn")
        toggle_btn.setToolTip("消息列表")
        toggle_btn.setFixedWidth(30)
        toggle_btn.clicked.connect(self._toggle_list)
        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("iconBtn")
        self._close_btn.setToolTip("收起")
        self._close_btn.setFixedWidth(30)
        self._close_btn.clicked.connect(self._on_close)
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)
        input_row.addWidget(self.quick_input, 1)
        input_row.addWidget(send_btn)
        input_row.addWidget(toggle_btn)
        input_row.addWidget(self._close_btn)
        self._input_box = QWidget()
        self._input_box.setLayout(input_row)

        # --- full list (panel) ---
        icon_label = QLabel("DSH")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet(
            "background: #2b6be4; color: white; border-radius: 5px;"
            "padding: 1px 6px; font-size: 10px; font-weight: bold;"
        )
        icon_label.setFixedHeight(18)
        self.status_label = QLabel("")
        self.status_label.setObjectName("headerStatus")
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 8, 0)
        header.setSpacing(6)
        header.addWidget(icon_label)
        header.addWidget(self.status_label, 1)

        self.list = QListWidget()
        self.list.setWordWrap(False)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._list_box = QFrame()
        self._list_box.setStyleSheet(
            "QFrame { background: rgba(255,255,255,235); border: none; border-radius: 12px; }"
        )
        list_layout = QVBoxLayout(self._list_box)
        list_layout.setContentsMargins(0, 2, 0, 6)
        list_layout.setSpacing(3)
        list_layout.addLayout(header)
        list_layout.addWidget(self.list, 1)

        # --- outer ---
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        outer.addWidget(self._pill)
        outer.addWidget(self._input_box)
        outer.addWidget(self._list_box)

        self._input_box.setVisible(False)
        self._list_box.setVisible(False)
        self.quick_input.submitted.connect(self._quick_send)
        self.quick_input.collapse_requested.connect(self.show_capsule)
        self.quick_input.textChanged.connect(self._on_input_text_changed)

        self._apply_mode("capsule")

    def _on_input_text_changed(self) -> None:
        """Let the quick input grow with its content (Shift+Enter newline)."""
        self.quick_input.auto_grow()
        if self._mode in ("expanded", "panel") and self.isVisible():
            self._resize()

    # ------------------------------------------------------------------ modes

    def _apply_mode(self, mode: str) -> None:
        self._mode = mode
        capsule_only = mode == "capsule"
        self._input_box.setVisible(not capsule_only)
        self._list_box.setVisible(mode == "panel")
        if mode == "capsule":
            self._remove_click_catcher()
        else:
            self._install_click_catcher()
        self._resize()

    def _resize(self) -> None:
        pill_h = self._pill.height()
        if self._mode == "capsule":
            w = CAPSULE_W
            h = pill_h + 16
        elif self._mode == "expanded":
            w = CAPSULE_W + 60
            h = pill_h + 6 + self._input_box.sizeHint().height() + 22
        else:
            w = PANEL_W
            h = PANEL_H
        self.setFixedSize(w, h)

    def show_capsule(self) -> None:
        """Default head-top pill."""
        self._apply_mode("capsule")
        if not self.isVisible():
            self.show()
            self.raise_()

    show_bubble = show_capsule  # legacy alias

    def show_expanded(self) -> None:
        """Capsule + quick input (clicked the pill)."""
        self._apply_mode("expanded")
        if not self.isVisible():
            self.show()
            self.raise_()
        self.activateWindow()
        self.quick_input.setFocus()

    def show_panel(self) -> None:
        """Full DSH message list (menu item / ☰)."""
        self._apply_mode("panel")
        if not self.isVisible():
            self.show()
            self.raise_()
        self.activateWindow()
        self.quick_input.setFocus()

    def _toggle_list(self) -> None:
        if self._mode == "panel":
            self.show_expanded()
        else:
            self.show_panel()

    def _on_close(self) -> None:
        if self._mode == "panel":
            self.show_expanded()
        else:
            self.show_capsule()

    def _on_pill_clicked(self) -> None:
        if self._mode == "capsule":
            self.show_expanded()
        else:
            self.quick_input.setFocus()

    def _quick_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.quick_input.clear()
        self.send_requested.emit(text)

    # ------------------------------------------------------------ click catcher

    def _install_click_catcher(self) -> None:
        app = QApplication.instance()
        if app is None or self._filter_app is not None:
            return
        self._filter_app = app
        app.installEventFilter(self)

    def _remove_click_catcher(self) -> None:
        app = self._filter_app
        if app is None:
            return
        self._filter_app = None
        app.removeEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if (
            self._mode in ("expanded", "panel")
            and event.type() == QEvent.Type.MouseButtonPress
            and self.isVisible()
        ):
            pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
            target = QApplication.widgetAt(pos)
            if target is not self and not self.isAncestorOf(target):
                # Click elsewhere (e.g. the pet, another window) closes input.
                QTimer.singleShot(0, self.show_capsule)
        return False

    # ------------------------------------------------------------------ position

    def position_near(self, root_x: float, head_y: float) -> None:
        """Pin the capsule just above the pet's head (centered on it)."""
        self._anchor = (root_x, head_y)
        w, h = self.width(), self.height()
        x = int(round(root_x - w / 2.0))
        y = int(round(head_y - h - 4.0))
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - w))
            y = max(geo.top(), y)
        # Only move when the position actually changed: repeated move() calls
        # would interrupt the IME composition while typing Chinese.
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    # ------------------------------------------------------------------ status + activity

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_activity(self, state) -> None:
        """Render an ActivityState on the capsule.

        Fixed width + word wrap: long text wraps onto more lines instead of
        being ellipsized away, so the summary is actually readable ("显示多一
        些 / 超出就分行"). Height grows with the line count, capped at
        MAX_CAPSULE_LINES; only beyond that cap do we fall back to a shorter
        summary (still semantic, never a mid-char hard cut).
        """
        self._state = state
        status = getattr(state, "status", "connected") or "connected"
        color = STATUS_COLORS.get(status, STATUS_COLORS["disconnected"])
        self._dot.set_color(color)
        self._dot.set_pulsing(status in PULSE_STATUS)
        self._pill.set_accent(WAIT_ACCENT if bool(getattr(state, "is_waiting_for_user", False)) else None)

        harness = getattr(state, "harness_display_name", "") or ""
        summary = getattr(state, "summary", "") or "思考中…"
        harness_w = self._harness_label.fontMetrics().horizontalAdvance(harness) if harness else 0
        # Text budget: capsule width minus dot/chevron/margins/spacings/harness.
        avail_w = CAPSULE_W - 32 - (harness_w + 6 if harness else 0)

        show_harness = bool(harness)
        if show_harness:
            display = f"{harness} · {summary}"
        else:
            display = summary
        if self._wrapped_lines(display, avail_w) > MAX_CAPSULE_LINES:
            # Drop the harness badge first, then shorten the summary.
            if show_harness:
                show_harness = False
                display = summary
                avail_w = CAPSULE_W - 32
            if self._wrapped_lines(display, avail_w) > MAX_CAPSULE_LINES:
                for keep in (14, 12, 10, 8, 6):
                    candidate = compress_summary_to(summary, keep)
                    if self._wrapped_lines(candidate, avail_w) <= MAX_CAPSULE_LINES:
                        display = candidate
                        break

        self._harness_label.setText(harness if show_harness else "")
        self._harness_label.setVisible(bool(show_harness))
        self._summary_label.setWordWrap(True)
        self._summary_label.setText(display)
        self._capsule_w = CAPSULE_W
        line_h = max(1, self._summary_label.fontMetrics().height())
        n_lines = self._wrapped_lines(display, avail_w)
        self._pill.setFixedHeight(max(CAPSULE_H, n_lines * line_h + 14))
        self._resize()

        # Tooltip: full un-shortened info, per §8.
        tip_harness = harness or (getattr(state, "harness_display_name", "") or "DSH")
        full = getattr(state, "full_activity", "") or summary
        lines = [f"Harness: {tip_harness}"]
        lines.append(f"状态: {getattr(state, 'status_zh', '') or status}")
        lines.append(f"当前任务: {full}")
        self._pill.setToolTip("\n".join(lines))
        self._summary_label.setToolTip("\n".join(lines))

        # Placeholder: "和 DSH 说点什么…" when a harness is attached.
        if getattr(state, "is_connected", False) and tip_harness:
            self.quick_input.setPlaceholderText(f"和 {tip_harness} 说点什么…")
        else:
            self.quick_input.setPlaceholderText("输入消息…")

    def _wrapped_lines(self, text: str, avail_w: int) -> int:
        """How many lines the text wraps into at the given width."""
        if not text:
            return 1
        fm = self._summary_label.fontMetrics()
        rect = fm.boundingRect(
            0, 0, max(1, avail_w), 3000, Qt.TextFlag.TextWordWrap, text
        )
        step = max(1, fm.height())
        return max(1, int(round(rect.height() / step)))

    # ------------------------------------------------------------ project picker

    def set_project_provider(self, choices_fn, select_fn, current_fn) -> None:
        """Wire the "项目" picker to the integration's session list."""
        self._choices_fn = choices_fn
        self._select_fn = select_fn
        self._current_fn = current_fn
        self._project_btn.setVisible(True)

    def _open_project_menu(self) -> None:
        if self._choices_fn is None or self._select_fn is None:
            return
        menu = QMenu(self)
        menu.setStyleSheet(PANEL_QSS)
        current = self._current_fn() if self._current_fn else None
        auto = menu.addAction("跟随当前活动")
        auto.setCheckable(True)
        auto.setChecked(current is None)
        auto.triggered.connect(lambda: self._select_fn(""))
        choices = self._choices_fn()
        menu.addSeparator()
        if not choices:
            empty = menu.addAction("没有其他 DSH 会话")
            empty.setEnabled(False)
        else:
            for choice in choices:
                sid = choice["session_id"]
                dot = "●" if choice.get("running") else "○"
                action = menu.addAction(f"{dot} {choice['label']}")
                action.setCheckable(True)
                action.setChecked(current == sid)
                action.triggered.connect(
                    lambda checked=False, _sid=sid: self._select_fn(_sid)
                )
        pos = self._project_btn.mapToGlobal(QPoint(0, self._project_btn.height()))
        menu.exec(pos)

    # ------------------------------------------------------------------ content

    def _row(self, role: str, text: str) -> str:
        label = ROLE_LABELS.get(role, role)
        return f"{label} {_clip(text)}"

    def _render_item(self, item: QListWidgetItem, role: str, text: str) -> None:
        """Tool rows get an SVG icon + bold title (DSH web UI style), others plain."""
        if role == "tool":
            widget = self.list.itemWidget(item)
            if widget is None:
                widget = QWidget()
                row = QHBoxLayout(widget)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(4)
                icon_label = QLabel()
                icon_label.setFixedSize(14, 14)
                row.addWidget(icon_label)
                text_label = QLabel()
                text_label.setTextFormat(Qt.TextFormat.RichText)
                row.addWidget(text_label, 1)
                item.setSizeHint(QSize(320, 20))
                self.list.setItemWidget(item, widget)
            else:
                row = widget.layout()
                icon_label = row.itemAt(0).widget()
                text_label = row.itemAt(1).widget()
            title, _, detail = text.partition(" · ")
            icon_file = TOOL_ICON_FILES.get(title, "terminal")
            svg_path = TOOL_ICONS_DIR / f"{icon_file}.svg"
            if svg_path.exists():
                icon_label.setPixmap(QIcon(str(svg_path)).pixmap(14, 14))
            else:
                icon_label.setPixmap(QPixmap())
            html = f"<b>{_clip(title, 12)}</b>"
            if detail:
                html += f' <span style="color:#66667c;">{_clip(detail, 26)}</span>'
            text_label.setText(html)
            widget.setToolTip(text)
        else:
            if self.list.itemWidget(item) is not None:
                self.list.setItemWidget(item, None)
            item.setText(self._row(role, text))
            item.setToolTip(text)

    def _trim_rows(self) -> None:
        """Keep the panel small: drop oldest rows, never question rows."""
        while self.list.count() > self.MAX_ROWS:
            removed = False
            for index in range(self.list.count()):
                item = self.list.item(index)
                if item.data(self.KEEP_ROLE) is True:
                    continue
                if item.data(Qt.ItemDataRole.UserRole) == "progress" and index == self.list.count() - 1:
                    continue  # keep the trailing progress line
                self.list.takeItem(index)
                removed = True
                break
            if not removed:
                break

    def append_message(self, role: str, text: str) -> None:
        item = QListWidgetItem()
        self.list.addItem(item)  # addItem BEFORE setItemWidget
        self._render_item(item, role, text)
        self.list.scrollToBottom()
        self._trim_rows()

    def set_row(self, tag: str, role: str, text: str) -> None:
        """Create or update the tagged row (streaming assistant text / tools)."""
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == tag:
                self._render_item(item, role, text)
                self.list.scrollToBottom()
                return
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, tag)
        self.list.addItem(item)  # addItem BEFORE setItemWidget
        self._render_item(item, role, text)
        self.list.scrollToBottom()
        self._trim_rows()

    def upsert_progress(self, text: str) -> None:
        """Update the single progress row anywhere in the list (never duplicate)."""
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == "progress":
                self._render_item(item, "progress", text)
                self.list.scrollToBottom()
                return
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, "progress")
        self.list.addItem(item)  # addItem BEFORE setItemWidget
        self._render_item(item, "progress", text)
        self.list.scrollToBottom()
        self._trim_rows()

    def append_question(self, question) -> None:
        """Render a pending user question as a row with answer buttons."""
        item = QListWidgetItem()
        item.setData(self.KEEP_ROLE, True)  # never trimmed until answered
        widget = QuestionRow(question, self._answer)
        item.setSizeHint(widget.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, widget)
        self.list.scrollToBottom()

    def _answer(self, question, selected: list[str], custom: str = "") -> None:
        self.answer_requested.emit(question, selected, custom)

    def clear(self) -> None:
        self.list.clear()
