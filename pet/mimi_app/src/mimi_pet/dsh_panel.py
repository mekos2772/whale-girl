"""DSH message bubble in two states.

* Bubble state (default): a small speech bubble like the pet's "搞定啦！"
  bubble — white rounded panel with a downward tail, showing at most 3 of
  the latest DSH messages. Clicking it expands into the full panel.
* Panel state: the complete mini DSH UI (header with live status, message
  list with DSH-style tool rows, input line). The ✕ button returns to the
  bubble state.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PANEL_QSS = """
QListWidget {
    background: rgba(255, 255, 255, 235);
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
QLabel#bubbleLabel {
    background: rgba(255, 255, 255, 235);
    border: none;
    border-radius: 10px;
    color: #1e1e1e;
    font-family: "Microsoft YaHei";
    font-size: 13px;
    padding: 6px 10px;
}
QLineEdit {
    background: rgba(250, 250, 252, 255);
    border: 1px solid rgba(185, 185, 202, 230);
    border-radius: 8px;
    color: #1e1e1e;
    font-family: "Microsoft YaHei";
    font-size: 12px;
    padding: 4px 8px;
}
QLineEdit:focus { border-color: rgba(91, 141, 239, 220); background: white; }
QPushButton {
    background: rgba(255, 255, 255, 165);
    border: none;
    border-radius: 8px;
    color: #44445a;
    font-family: "Microsoft YaHei";
    font-size: 12px;
    padding: 3px 10px;
}
QPushButton:hover { background: rgba(255, 255, 255, 215); }
QPushButton:checked { background: rgba(230, 225, 255, 225); color: #4a3f8f; }
QPushButton#closeBtn {
    background: transparent;
    border: none;
    color: #9a9aae;
    padding: 0px 6px;
    font-size: 12px;
}
QPushButton#closeBtn:hover { background: rgba(220, 80, 80, 170); color: white; }
QPushButton#answerBtn {
    background: rgba(91, 141, 239, 210);
    border: none;
    border-radius: 8px;
    color: white;
    padding: 3px 10px;
}
QPushButton#answerBtn:hover { background: rgba(110, 160, 250, 230); }
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

# Single-line display budget (chars) inside the small bubble.
MAX_CHARS = 34
BUBBLE_MAX_CHARS = 34
RECENT_KEEP = 5
PANEL_W, PANEL_H = 380, 216
# Fixed bubble size: never jumps regardless of message length.
BUBBLE_W, BUBBLE_H = 300, 58


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


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
                confirm.setObjectName("answerBtn")
                confirm.clicked.connect(self._confirm)
                row.addWidget(confirm)
            layout.addLayout(row)
        else:
            custom = QLineEdit()
            custom.setPlaceholderText("输入回答，回车发送…")
            custom.returnPressed.connect(self._custom_send)
            layout.addWidget(custom)

    def _toggle(self, btn: QPushButton, checked: bool, label: str) -> None:
        if checked and label not in self._selected:
            self._selected.append(label)
        elif not checked and label in self._selected:
            self._selected.remove(label)

    def _confirm(self) -> None:
        self._answer(self._selected)

    def _custom_send(self) -> None:
        widget = self.sender()
        if isinstance(widget, QLineEdit):
            self._emit(self._question, [], widget.text().strip())

    def _answer(self, selected: list[str]) -> None:
        self._emit(self._question, selected)


class DshPanel(QWidget):
    """Speech bubble (3 latest messages) that expands into the full DSH panel."""

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
        self._panel_mode = False
        self._recent: list[dict] = []  # {tag, role, text}

        # --- bubble state: like the pet's "搞定啦！" bubble, 3 lines max ---
        self.bubble_label = QLabel("DSH 空闲")
        self.bubble_label.setObjectName("bubbleLabel")
        self.bubble_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bubble_box = QWidget()
        bubble_layout = QVBoxLayout(self._bubble_box)
        bubble_layout.setContentsMargins(0, 0, 0, 14)  # room for the tail
        bubble_layout.addWidget(self.bubble_label)

        # --- panel state: full mini DSH UI ---
        icon_label = QLabel("DSH")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet(
            "background: #2b6be4; color: white; border-radius: 5px;"
            "padding: 1px 6px; font-size: 10px; font-weight: bold;"
        )
        icon_label.setFixedHeight(18)
        self.status_label = QLabel("")
        self.status_label.setObjectName("headerStatus")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedWidth(22)
        close_btn.clicked.connect(self.show_bubble)

        header = QHBoxLayout()
        header.setContentsMargins(6, 2, 6, 0)
        header.setSpacing(6)
        header.addWidget(icon_label)
        header.addWidget(self.status_label, 1)
        header.addWidget(close_btn)

        self.list = QListWidget()
        self.list.setWordWrap(False)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.input = QLineEdit()
        self.input.setPlaceholderText("发消息…")
        self.input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.input.returnPressed.connect(self._send)
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self._send)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(8, 4, 8, 4)
        input_row.setSpacing(6)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(send_btn)

        def separator() -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color: rgba(205, 205, 220, 180);")
            return line

        self._panel_box = QWidget()
        panel_layout = QVBoxLayout(self._panel_box)
        panel_layout.setContentsMargins(0, 0, 0, 14)  # room for the tail
        panel_layout.setSpacing(3)
        panel_layout.addLayout(header)
        panel_layout.addWidget(separator())
        panel_layout.addWidget(self.list, 1)
        panel_layout.addWidget(separator())
        panel_layout.addLayout(input_row)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 0)
        layout.setSpacing(0)
        layout.addWidget(self._bubble_box)
        layout.addWidget(self._panel_box)
        self.setLayout(layout)

        self._bubble_box.setVisible(True)
        self._panel_box.setVisible(False)
        self.adjustSize()

    # ------------------------------------------------------------------ states

    def show_bubble(self) -> None:
        """Bubble state: fixed-size speech bubble with the latest message."""
        self._panel_mode = False
        self._panel_box.setVisible(False)
        self._bubble_box.setVisible(True)
        self.setFixedSize(BUBBLE_W, BUBBLE_H)
        if not self.isVisible():
            self.show()
            self.raise_()

    def show_panel(self) -> None:
        """Full panel state (clicked the bubble / user request)."""
        self._panel_mode = True
        self._bubble_box.setVisible(False)
        self._panel_box.setVisible(True)
        self.setFixedSize(PANEL_W, PANEL_H)
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
        self.input.setFocus()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Clicking the bubble expands it into the full panel."""
        if not self._panel_mode:
            self.show_panel()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------ position

    def position_near(self, root_x: float, head_y: float) -> None:
        """Pin the panel just above the pet's head (tail points down at it)."""
        self._anchor = (root_x, head_y)
        w, h = self.width(), self.height()
        x = int(round(root_x - w / 2.0))
        y = int(round(head_y - h - 8.0))
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - w))
            y = max(geo.top(), y)
        # Only move when the position actually changed: repeated move() calls
        # would interrupt the IME composition while typing Chinese.
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    # ------------------------------------------------------------------ tail

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Speech-bubble tail pointing down toward the pet."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235))
        cx = self.width() / 2.0
        bottom = float(self.height() - 1)
        tail = QPolygonF(
            [
                QPointF(cx - 7, bottom - 10),
                QPointF(cx + 7, bottom - 10),
                QPointF(cx, bottom),
            ]
        )
        painter.drawPolygon(tail)
        painter.end()

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.send_requested.emit(text)
            self.input.clear()

    # ------------------------------------------------------------------ status

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # ------------------------------------------------------------------ content

    def _remember(self, tag: str | None, role: str, text: str) -> None:
        if tag:
            for entry in self._recent:
                if entry["tag"] == tag:
                    entry["role"], entry["text"] = role, text
                    return
        self._recent.append({"tag": tag, "role": role, "text": text})
        if len(self._recent) > RECENT_KEEP:
            del self._recent[0]

    def _bubble_line(self, role: str, text: str) -> str:
        text = _clip(text, BUBBLE_MAX_CHARS)
        if role == "tool":
            return text  # already "Edit · path"
        return f"{ROLE_LABELS.get(role, role)} {text}"

    def _update_bubble(self) -> None:
        """Bubble shows only the LATEST message (Codex-pet style: one line at a
        time, replaced on new activity — no stacking, fixed size)."""
        if self._recent:
            latest = self._recent[-1]
            self.bubble_label.setText(self._bubble_line(latest["role"], latest["text"]))
        else:
            self.bubble_label.setText("DSH 空闲")
        if self.isVisible() and not self._panel_mode and self._anchor is not None:
            self.position_near(*self._anchor)

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
        self._remember(None, role, text)
        self._update_bubble()
        item = QListWidgetItem()
        self.list.addItem(item)  # addItem BEFORE setItemWidget
        self._render_item(item, role, text)
        self.list.scrollToBottom()
        self._trim_rows()

    def set_row(self, tag: str, role: str, text: str) -> None:
        """Create or update the tagged row (streaming assistant text / tools)."""
        self._remember(tag, role, text)
        self._update_bubble()
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
        self._remember("progress", "progress", text)
        self._update_bubble()
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
        self._remember("question", "question", question.question)
        self._update_bubble()
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
        self._recent = []
        self._update_bubble()
