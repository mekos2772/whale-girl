"""Transient DSH speech bubbles above the pet: one bubble per message.

The built-in action bubble (engine.set_external_bubble) lives inside the pet
window; this layer is its DSH counterpart — a stack of clickable white
chips floating above the head (and above the status capsule when visible).
Each bubble expires on its own (hover pauses the countdown); clicking one
opens the full chat card.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPolygonF,
)
from PySide6.QtWidgets import QVBoxLayout, QWidget

MAX_BUBBLES = 3
DEFAULT_LIFETIME_S = 6.0
BUBBLE_MAX_W = 300.0
BUBBLE_PAD_X = 11.0
BUBBLE_PAD_Y = 7.0
BUBBLE_TAIL_H = 7.0
MAX_LINES = 3

ACCENTS = {
    # waiting for the user: same orange the capsule uses
    "question": QColor(0xFF, 0x9F, 0x43),
    # reply summary: teal
    "summary": QColor(0x5F, 0xD3, 0xB0),
}


def _wrap(text: str, metrics: QFontMetrics, max_width: int) -> list[str]:
    """Greedy per-character wrap (CJK-safe); capped at MAX_LINES."""
    lines: list[str] = []
    current = ""
    for ch in text:
        if metrics.horizontalAdvance(current + ch) > max_width and current:
            lines.append(current)
            current = ch
            if len(lines) == MAX_LINES:
                break
        else:
            current += ch
    if len(lines) < MAX_LINES and current:
        lines.append(current)
    # Ellipsis when the wrap hit the line cap before consuming everything.
    consumed = sum(len(line) for line in lines)
    if consumed < len(text) and lines:
        lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    return lines or [""]


class BubbleChip(QWidget):
    """One white speech bubble: hover pauses expiry, click opens the card."""

    clicked = Signal()

    def __init__(
        self,
        text: str,
        kind: str = "assistant",
        lifetime_s: float = DEFAULT_LIFETIME_S,
    ) -> None:
        super().__init__()
        self._full = (text or "").strip()
        self._kind = kind
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self._full)

        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(12)
        self._font = font
        metrics = QFontMetrics(font)
        self._lines = _wrap(self._full, metrics, int(BUBBLE_MAX_W - BUBBLE_PAD_X * 2))
        text_w = max(metrics.horizontalAdvance(line) for line in self._lines)
        self._w = min(BUBBLE_MAX_W, text_w + BUBBLE_PAD_X * 2)
        self._h = metrics.height() * len(self._lines) + BUBBLE_PAD_Y * 2 + BUBBLE_TAIL_H
        self.setFixedSize(int(self._w), int(self._h))

        self._remaining_s = lifetime_s
        self._hover_confirmed = False
        self.setMouseTracking(True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expired)
        self._timer.start(int(lifetime_s * 1000))

    expired = Signal(object)

    def _expired(self) -> None:
        self.expired.emit(self)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # A shown widget also receives synthetic enter events (e.g. offscreen
        # tests, or the layer appearing under the pointer), so the countdown
        # only pauses once a real mouse move confirms the hover.
        self._hover_confirmed = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if not self._hover_confirmed:
            self._hover_confirmed = True
            self._remaining_s = max(1.0, self._timer.remainingTime() / 1000.0)
            self._timer.stop()

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._hover_confirmed:
            self._hover_confirmed = False
            self._timer.start(int(self._remaining_s * 1000))

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            self._timer.stop()
            self._expired()
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(0.5, 0.5, self._w - 1.0, self._h - BUBBLE_TAIL_H - 0.5)
        radius = 10.0
        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        painter.fillPath(path, QColor(255, 255, 255, 242))
        accent = ACCENTS.get(self._kind)
        painter.setPen(accent if accent is not None else QColor(0, 0, 0, 26))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        # Tail pointing down toward the pet.
        cx = self._w / 2.0
        tail = QPolygonF(
            [
                QPointF(cx - 7, body.bottom() - 1),
                QPointF(cx + 7, body.bottom() - 1),
                QPointF(cx, body.bottom() + BUBBLE_TAIL_H - 1),
            ]
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 242))
        painter.drawPolygon(tail)
        painter.setFont(self._font)
        painter.setPen(QColor(38, 42, 56))
        rect = body.adjusted(BUBBLE_PAD_X, BUBBLE_PAD_Y, -BUBBLE_PAD_X, -BUBBLE_PAD_Y)
        for index, line in enumerate(self._lines):
            line_rect = QRectF(
                rect.left(),
                rect.top() + index * (rect.height() / len(self._lines)),
                rect.width(),
                rect.height() / len(self._lines),
            )
            painter.drawText(
                line_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )
        painter.end()


class BubbleLayer(QWidget):
    """Stack of transient bubbles floating above the pet's head."""

    message_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._chips: list[BubbleChip] = []
        self._anchor: tuple[float, float] | None = None
        self.hide()

    # ------------------------------------------------------------------ API

    def add_bubble(
        self,
        text: str,
        kind: str = "assistant",
        lifetime_s: float = DEFAULT_LIFETIME_S,
    ) -> None:
        if not (text or "").strip():
            return
        chip = BubbleChip(text, kind, lifetime_s)
        chip.expired.connect(self._drop)
        chip.clicked.connect(self.message_clicked)
        self._chips.append(chip)  # newest ends up closest to the pet
        self._layout.addWidget(chip)
        while len(self._chips) > MAX_BUBBLES:
            self._remove(self._chips[0])
        self._relayout()

    def bubbles(self) -> tuple[BubbleChip, ...]:
        return tuple(self._chips)

    def _drop(self, chip: BubbleChip) -> None:
        if chip in self._chips:
            self._remove(chip)

    def _remove(self, chip: BubbleChip) -> None:
        self._chips.remove(chip)
        self._layout.removeWidget(chip)
        chip.deleteLater()
        self._relayout()

    def _relayout(self) -> None:
        if not self._chips:
            self.hide()
            return
        height = sum(chip.height() for chip in self._chips) + 4 * max(0, len(self._chips) - 1)
        width = max(chip.width() for chip in self._chips)
        self.setFixedSize(width, height)
        # Position before the first show: otherwise the stack appears at its
        # stale location for one tick until the next position_above call.
        self._apply_anchor()
        if not self.isVisible():
            self.show()
            self.raise_()

    def position_above(self, root_x: float, bottom_y: float) -> None:
        """Remember the anchor and pin the stack right above ``bottom_y``."""
        self._anchor = (root_x, bottom_y)
        self._apply_anchor()

    def _apply_anchor(self) -> None:
        if self._anchor is None:
            return
        root_x, bottom_y = self._anchor
        x = int(round(root_x - self.width() / 2.0))
        y = int(round(bottom_y - self.height() - 4.0))
        screen = QGuiApplication.screenAt(QPoint(int(root_x), int(bottom_y)))
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - self.width()))
            y = max(geo.top(), y)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
