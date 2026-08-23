"""Transparent, frameless, always-on-top pet window.

The window never mutates the state machine: mouse events send domain events
to the engine, and paintEvent only consumes the latest RenderSnapshot.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPolygonF,
)
from PySide6.QtWidgets import QMenu, QSlider, QWidget, QWidgetAction

from .collision import WorkArea, ground_for_point
from .debug_overlay import draw_debug_overlay
from .engine import PetEngine, touch_region

# Dropped picture files count as treats (bread) for the pet.
FEED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
from .renderer import QtRenderer, RenderSnapshot
from .state_machine import PetState


class MimiWindow(QWidget):
    closed = Signal()

    def __init__(
        self,
        engine: PetEngine,
        renderer: QtRenderer,
        ground_provider: callable,
        bounds_provider: callable,
        size_changed: callable | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.renderer = renderer
        self.ground_provider = ground_provider
        self.bounds_provider = bounds_provider
        self.size_changed = size_changed
        self.dsh = None
        self._snapshot: RenderSnapshot | None = None
        self._pixmap = None
        self.debug_enabled = False
        self._dragging = False
        # Long-press-to-drag: a press arms a timer; dragging only starts when
        # the mouse is held still for long_press_s or moves beyond
        # drag_start_move_px (instant grab for fast drags). A plain click
        # never drags.
        self._press_armed = False
        self._press_pos = None
        self._press_local = None
        self._long_press_s = float(engine.drag.config.long_press_s)
        self._start_move_px = float(engine.drag.config.drag_start_move_px)
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(int(self._long_press_s * 1000))
        self._long_press_timer.timeout.connect(self._on_long_press)

        # Double-click detection (two clicks < 320 ms apart on the pet).
        self._last_release_at = 0.0
        self._double_click_ms = 320
        # Welcome back: a pointer returning after a long absence waves hello.
        # Hover itself stays silent — cursor attention is continuous (Live
        # gaze tracking), only the leave→enter gap carries meaning.
        self._pointer_left_at: float | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.resize(engine.display_w, engine.display_h)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------ rendering

    def show_frame(self, snapshot: RenderSnapshot, pixmap) -> None:
        self._snapshot = snapshot
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        if self.debug_enabled and self._snapshot is not None:
            draw_debug_overlay(painter, self.rect(), self._snapshot)
        if self._snapshot is not None and self._snapshot.bubble_text:
            self._draw_bubble(painter, self._snapshot.bubble_text)
        painter.end()

    def _draw_bubble(self, painter: QPainter, text: str) -> None:
        """Speech bubble above the character's head."""
        font = QFont("Microsoft YaHei")
        font.setPixelSize(13)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        padding_x, padding_y = 10, 7
        bubble_w = text_width + padding_x * 2
        bubble_h = metrics.height() + padding_y * 2
        bubble_x = (self.width() - bubble_w) / 2.0
        bubble_y = 6.0
        painter.save()
        painter.setFont(font)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(QRectF(bubble_x, bubble_y, bubble_w, bubble_h), 10.0, 10.0)
        # Tail pointing down toward the character.
        tail = QPolygonF(
            [
                QPointF(bubble_x + bubble_w / 2.0 - 8, bubble_y + bubble_h - 1),
                QPointF(bubble_x + bubble_w / 2.0 + 8, bubble_y + bubble_h - 1),
                QPointF(bubble_x + bubble_w / 2.0, bubble_y + bubble_h + 8),
            ]
        )
        painter.drawPolygon(tail)
        painter.setPen(QColor(30, 30, 30))
        painter.drawText(
            QRectF(bubble_x, bubble_y, bubble_w, bubble_h),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()

    # ------------------------------------------------------------------ mouse

    def _visible_at(self, local_pos: QPoint) -> bool:
        """Alpha hit test: only the visible character pixels grab the mouse."""
        if self._pixmap is None:
            return False
        if not (0 <= local_pos.x() < self.width() and 0 <= local_pos.y() < self.height()):
            return False
        image = self._pixmap.toImage()
        return image.pixelColor(local_pos).alpha() > 0

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton and self._visible_at(event.position().toPoint()):
            self._dragging = True
            self._press_armed = True
            self._press_pos = event.globalPosition()
            self._press_local = event.position()
            self._long_press_timer.start()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def _begin_drag(self, x: float, y: float) -> None:
        self._press_armed = False
        self._long_press_timer.stop()
        self.engine.begin_drag(x, y, time.perf_counter())
        self.grabMouse()

    def _on_long_press(self) -> None:
        if not self._press_armed:
            return
        cursor = QCursor.pos()
        self._begin_drag(float(cursor.x()), float(cursor.y()))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.engine.states.state is PetState.DRAGGING:
            self.engine.update_drag(
                event.globalPosition().x(),
                event.globalPosition().y(),
                time.perf_counter(),
                bounds=self.bounds_provider(),
            )
            self._move_to_root()
            event.accept()
        elif self._press_armed and self._press_pos is not None:
            # Moving beyond the threshold while pressing starts the drag
            # immediately (fast drags must not wait for the long press).
            delta = event.globalPosition() - self._press_pos
            if abs(delta.x()) > self._start_move_px or abs(delta.y()) > self._start_move_px:
                self._begin_drag(event.globalPosition().x(), event.globalPosition().y())
                self.engine.update_drag(
                    event.globalPosition().x(),
                    event.globalPosition().y(),
                    time.perf_counter(),
                    bounds=self.bounds_provider(),
                )
                self._move_to_root()
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._press_armed:
            # Plain click: never drags, but gives a soft reaction.
            self._press_armed = False
            self._long_press_timer.stop()
            self._press_pos = None
            self._dragging = False
            now_s = time.perf_counter()
            double_click = (now_s - self._last_release_at) * 1000.0 < self._double_click_ms
            self._last_release_at = now_s
            if double_click:
                self.engine.trigger_double_click()
            else:
                local = self._press_local or event.position()
                x_ratio = local.x() / max(1.0, float(self.width()))
                y_ratio = local.y() / max(1.0, float(self.height()))
                self.engine.trigger_touch(
                    touch_region(x_ratio, y_ratio), x_ratio=x_ratio
                )
            self._press_local = None
            event.accept()
            return
        if self.engine.states.state is PetState.DRAGGING:
            ground = self.ground_provider(self.engine.root_x, self.engine.root_y)
            self.engine.release(ground, time.perf_counter())
            self._dragging = False
            self.releaseMouse()
            event.accept()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().enterEvent(event)
        if self._dragging or self._press_armed or self._pointer_left_at is None:
            return
        away_s = time.perf_counter() - self._pointer_left_at
        self._pointer_left_at = None
        self.engine.trigger_welcome_back(away_s)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().leaveEvent(event)
        self._pointer_left_at = time.perf_counter()

    def _move_to_root(self) -> None:
        self.move(
            int(round(self.engine.root_x - self.engine.pivot_display_x)),
            int(round(self.engine.root_y - self.engine.pivot_display_y)),
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        mime = event.mimeData()
        if mime.hasUrls():
            # Pictures are treats: dropping an image feeds her bread.
            paths = [url.toLocalFile() for url in mime.urls()]
            if any(path.lower().endswith(FEED_IMAGE_EXTS) for path in paths):
                if self.engine.feed_bread():
                    event.acceptProposedAction()
                    return
        if self.engine.receive_drop():
            event.acceptProposedAction()
        else:
            event.ignore()

    # ------------------------------------------------------------------ menu

    MENU_QSS = """
    QMenu {
        background-color: rgba(25, 30, 48, 248);
        border: 1px solid rgba(126, 151, 218, 85);
        border-radius: 12px;
        padding: 7px;
        font-family: "Microsoft YaHei UI", "Microsoft YaHei";
        font-size: 12px;
    }
    QMenu::item {
        background: transparent;
        color: #edf2ff;
        padding: 7px 30px 7px 13px;
        border-radius: 7px;
        margin: 1px 3px;
    }
    QMenu::item:selected {
        background: rgba(95, 134, 237, 165);
    }
    QMenu::item:disabled {
        color: #69738f;
    }
    QMenu::separator {
        height: 1px;
        background: rgba(126, 151, 218, 55);
        margin: 5px 12px;
    }
    QMenu::indicator {
        width: 14px;
        height: 14px;
        margin-left: 4px;
    }
    QMenu::indicator:checked {
        image: none;
        background: #7198ff;
        border-radius: 3px;
    }
    """

    def _show_context_menu(self, global_pos: QPoint) -> None:
        if self.engine.states.state in (PetState.DRAGGING, PetState.FALLING):
            return
        menu = QMenu(self)
        menu.setStyleSheet(self.MENU_QSS)

        # 动作一律由交互触发（分区点击/双击/拖放/DSH 事件/久坐久睡场景），
        # 菜单只保留功能入口：投喂、尺寸与位置、Harness。
        feed_action = menu.addAction("投喂圆面包")
        feed_action.triggered.connect(self.engine.feed_bread)
        menu.addSeparator()

        # 尺寸、停靠和调试显示集中到一个设置菜单。
        settings_menu = menu.addMenu("尺寸与位置")
        settings_menu.setStyleSheet(self.MENU_QSS)
        size_group = QActionGroup(settings_menu)
        size_group.setExclusive(True)
        for label, (width, height) in (
            ("小号（192×288）", (192, 288)),
            ("中号（256×384）", (256, 384)),
            ("大号（320×480）", (320, 480)),
        ):
            size_item = settings_menu.addAction(label)
            size_item.setCheckable(True)
            size_item.setChecked(self.engine.display_w == width)
            size_group.addAction(size_item)
            size_item.triggered.connect(
                lambda checked=False, w=width, h=height: self._apply_size(w, h)
            )
        settings_menu.addSeparator()
        slide_action = QWidgetAction(settings_menu)
        slide_row = QWidget()
        from PySide6.QtWidgets import QHBoxLayout, QLabel

        slide_row.setStyleSheet(
            "QLabel { color: #cbd7f5; font-size: 11px; }"
            "QSlider::groove:horizontal { height: 4px; background: #303954; "
            "border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: #7198ff; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #f2f5ff; width: 12px; "
            "margin: -4px 0; border-radius: 6px; }"
        )
        slide_layout = QHBoxLayout(slide_row)
        slide_layout.setContentsMargins(6, 2, 6, 2)
        slide_layout.setSpacing(4)
        slide_layout.addWidget(QLabel("缩放"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(self.ZOOM_MIN_H, self.ZOOM_MAX_H)
        slider.setValue(self.engine.display_h)
        slider.setFixedWidth(150)
        slide_layout.addWidget(slider)
        zoom_value = QLabel(f"高 {self.engine.display_h}px")
        slider.valueChanged.connect(
            lambda h: self._apply_size(int(round(h * 2 / 3)), h)
        )
        slider.valueChanged.connect(lambda h: zoom_value.setText(f"高 {h}px"))
        slide_layout.addWidget(zoom_value)
        slide_action.setDefaultWidget(slide_row)
        settings_menu.addAction(slide_action)
        settings_menu.addSeparator()
        placement_group = QActionGroup(settings_menu)
        placement_group.setExclusive(True)
        dock_action = settings_menu.addAction("停靠屏幕底部")
        dock_action.setCheckable(True)
        dock_action.setChecked(not self.engine.free_placement)
        placement_group.addAction(dock_action)
        dock_action.toggled.connect(lambda checked: self.engine.set_free_placement(not checked))
        free_action = settings_menu.addAction("自由放置")
        free_action.setCheckable(True)
        free_action.setChecked(self.engine.free_placement)
        placement_group.addAction(free_action)
        free_action.toggled.connect(self.engine.set_free_placement)
        reset_action = settings_menu.addAction("回到屏幕底部")
        reset_action.triggered.connect(self._reset_position)
        debug_action = settings_menu.addAction("显示调试信息")
        debug_action.setCheckable(True)
        debug_action.setChecked(self.debug_enabled)
        debug_action.toggled.connect(self._set_debug_enabled)

        # DSH 集成
        if self.dsh is not None:
            dsh_menu = menu.addMenu("Harness")
            dsh_menu.setStyleSheet(self.MENU_QSS)
            # 模式：DSH 联动 = 监听工作会话；桌宠 Agent = 影子会话聊天。
            mode_group = QActionGroup(dsh_menu)
            mode_group.setExclusive(True)
            for label, value in (("DSH 联动", "link"), ("桌宠 Agent", "agent")):
                mode_item = dsh_menu.addAction(label)
                mode_item.setCheckable(True)
                mode_item.setChecked(self.dsh.mode == value)
                mode_group.addAction(mode_item)
                mode_item.triggered.connect(
                    lambda checked=False, m=value, item=mode_item: self._dsh_set_mode(item, m)
                )
            dsh_menu.addSeparator()
            show_panel_action = dsh_menu.addAction("打开消息面板")
            show_panel_action.triggered.connect(self._dsh_show_panel)
            self._build_project_menu(dsh_menu)
            cot_menu = dsh_menu.addMenu("摘要模型")
            cot_menu.setStyleSheet(self.MENU_QSS)
            self._build_cot_menu(cot_menu)
            if self.dsh.pending_questions:
                questions_menu = dsh_menu.addMenu(f"回答 DSH 问题（{len(self.dsh.pending_questions)}）")
                questions_menu.setStyleSheet(self.MENU_QSS)
                for question in list(self.dsh.pending_questions):
                    q_item = questions_menu.addAction(question.question[:30])
                    q_item.setToolTip(question.detail or question.question)
                    q_item.triggered.connect(
                        lambda checked=False, q=question: self._dsh_answer(q)
                    )
            if self.dsh.last_error:
                status_item = dsh_menu.addAction(f"未连接：{self.dsh.last_error[:30]}")
                status_item.setEnabled(False)

        menu.addSeparator()
        quit_action = menu.addAction("退出 Mimi")
        quit_action.triggered.connect(self.close)
        menu.exec(global_pos)

    def set_dsh_integration(self, dsh) -> None:
        self.dsh = dsh

    def set_project_provider(self, choices_fn, select_fn, current_fn) -> None:
        """Wire the Harness 项目 picker to the integration's session list."""
        self._project_choices = choices_fn
        self._project_select = select_fn
        self._project_current = current_fn

    def _build_project_menu(self, dsh_menu) -> None:
        """DSH 联动模式：选择跟随哪个项目（会话）。"""
        if getattr(self, "_project_choices", None) is None or self.dsh.mode != "link":
            return
        project_menu = dsh_menu.addMenu("项目")
        project_menu.setStyleSheet(self.MENU_QSS)
        current = self._project_current() if self._project_current else None
        auto = project_menu.addAction("跟随当前活动")
        auto.setCheckable(True)
        auto.setChecked(current is None)
        auto.triggered.connect(lambda: self._project_select(""))
        project_menu.addSeparator()
        choices = self._project_choices()
        if not choices:
            empty = project_menu.addAction("没有其他 DSH 会话")
            empty.setEnabled(False)
            return
        for choice in choices:
            sid = choice["session_id"]
            dot = "●" if choice.get("running") else "○"
            action = project_menu.addAction(f"{dot} {choice['label']}")
            action.setCheckable(True)
            action.setChecked(current == sid)
            action.triggered.connect(
                lambda checked=False, _sid=sid: self._project_select(_sid)
            )

    def _dsh_prompt(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "给 DSH 发消息", "消息内容：")
        if ok and text.strip() and self.dsh is not None:
            self.dsh.prompt_active(text.strip())

    def _dsh_show_panel(self) -> None:
        if self.dsh is not None:
            self.dsh.request_show_panel()

    def _dsh_set_mode(self, action, mode: str) -> None:
        """Switch DSH link / pet-agent mode; reset the tick on failure."""
        if self.dsh is None:
            return
        if not self.dsh.set_mode(mode):
            action.setChecked(False)

    def _build_cot_menu(self, cot_menu) -> None:
        """思维链总结模型：自动跟随 DSH / 指定模型 / 关闭。"""
        if self.dsh is None or self.dsh.cot is None:
            return
        current = (self.dsh.cot.provider, self.dsh.cot.model)
        for label, provider, model in self.dsh.cot_model_choices():
            action = cot_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(
                bool(self.dsh.cot.enabled) and (provider, model) == current
            )
            action.triggered.connect(
                lambda checked=False, p=provider, m=model: self.dsh.configure_cot(p, m)
            )
        cot_menu.addSeparator()
        off_action = cot_menu.addAction("关闭总结")
        off_action.setCheckable(True)
        off_action.setChecked(not self.dsh.cot.enabled)
        off_action.triggered.connect(
            lambda checked=False: self.dsh.configure_cot("auto", "", enabled=False)
        )

    def _dsh_answer(self, question) -> None:
        if self.dsh is None:
            return
        options = [opt.get("label") for opt in question.options] or [question.question]
        if question.multi_select:
            from PySide6.QtWidgets import QInputDialog

            chosen, ok = QInputDialog.getItem(
                self, "回答 DSH 问题", question.question, options, 0, False
            )
            if ok:
                self.dsh.answer_question(question, [chosen])
        else:
            from PySide6.QtWidgets import QInputDialog

            chosen, ok = QInputDialog.getItem(
                self, "回答 DSH 问题", question.question, options, 0, False
            )
            if ok:
                self.dsh.answer_question(question, [chosen])

    def _apply_size(self, width: int, height: int) -> None:
        """Window-style uniform scaling of the pet."""
        self.engine.set_display_size(width, height)
        if self.size_changed is not None:
            self.size_changed()
        self.resize(width, height)
        self._move_to_root()
        self.update()

    # ------------------------------------------------------------------ zoom (无级缩放)

    ZOOM_MIN_H = 160
    ZOOM_MAX_H = 640
    ZOOM_STEP_H = 16

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Mouse wheel scales the pet continuously (keeps the 2:3 window ratio)."""
        if self.engine.states.state in (PetState.DRAGGING, PetState.FALLING):
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        new_h = self.engine.display_h + (1 if delta > 0 else -1) * self.ZOOM_STEP_H
        new_h = max(self.ZOOM_MIN_H, min(self.ZOOM_MAX_H, new_h))
        if new_h != self.engine.display_h:
            self._apply_size(int(round(new_h * 2 / 3)), new_h)
        event.accept()

    def _reset_position(self) -> None:
        """Move the pet back to the bottom centre of the primary screen."""
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.engine.place_at(available.center().x(), float(available.bottom()))
        self._move_to_root()

    def _set_debug_enabled(self, enabled: bool) -> None:
        self.debug_enabled = enabled
        self.update()

    # ------------------------------------------------------------------ close

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        from .state_machine import Event

        self.engine.states.dispatch(Event.CLOSE)
        self.closed.emit()
        super().closeEvent(event)
