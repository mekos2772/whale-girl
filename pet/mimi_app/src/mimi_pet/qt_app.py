"""Qt application assembly: QApplication, 60 Hz ticker, scheduler timer and
window wiring. ``python -m mimi_pet`` launches this by default.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QApplication

from .action_library import ActionLibrary
from .collision import WorkArea, ground_for_point, union_bounds
from .config import load_config
from .dsh_integration import DshIntegration
from .dsh_panel import DshPanel
from .engine import PetEngine
from .image_cache import ImageCache
from .qt_window import MimiWindow
from .renderer import QtRenderer, build_snapshot
from .rig_model import load_rig_model
from .state_machine import PetState

TICK_INTERVAL_MS = 16  # ~60 Hz logic/render loop
MAX_DT_S = 0.1


def qt_work_areas() -> tuple[WorkArea, ...]:
    """All monitors' available desktop rectangles in virtual screen coords."""
    areas = []
    for screen in QGuiApplication.screens():
        geometry = screen.availableGeometry()
        areas.append(WorkArea(geometry.x(), geometry.y(), geometry.width(), geometry.height()))
    return tuple(areas)


def ground_y_at(root_x: float, root_y: float) -> float | None:
    area = ground_for_point(qt_work_areas(), root_x, root_y)
    return area.bottom if area is not None else None


def desktop_bounds() -> WorkArea | None:
    """Bounding rectangle of every work area (the drag cage)."""
    return union_bounds(qt_work_areas())


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Mimi 桌宠")
    app.setQuitOnLastWindowClosed(True)

    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    engine = PetEngine(library, config)
    rig_model_path = Path(config["asset_manifest"]).parent / library.live_rig["model"]
    rig = load_rig_model(rig_model_path)
    cache = ImageCache()
    renderer_holder: dict[str, QtRenderer] = {}

    def build_renderer() -> QtRenderer:
        return QtRenderer(
            cache,
            rig,
            (engine.display_w, engine.display_h),
            config.get("rig"),
            gaze_translation_scale=float(config["live"].get("gaze_translation_scale", 0.35)),
            gaze_hair_counter_scale=float(config["live"].get("gaze_hair_counter_scale", 0.3)),
        )

    renderer_holder["renderer"] = build_renderer()

    def rebuild_renderer() -> None:
        renderer_holder["renderer"] = build_renderer()

    window = MimiWindow(
        engine,
        renderer_holder["renderer"],
        library,
        ground_y_at,
        desktop_bounds,
        size_changed=rebuild_renderer,
    )

    # DSH integration: status polling + live event stream + message bar.
    integration = DshIntegration(engine)
    panel = DshPanel()
    integration.sink = panel
    panel.send_requested.connect(integration.prompt_active)
    panel.answer_requested.connect(integration.answer_question)
    # Multi-project: the capsule "项目" picker lists every DSH session and
    # lets the user pin which project is displayed / messaged.
    panel.set_project_provider(
        integration.session_choices,
        integration.select_session,
        integration.pinned_session,
    )

    def pet_head_y() -> float:
        """Character's actual head position (height ~220 display px, scaled)."""
        return engine.root_y - 220.0 * engine.display_h / 384.0

    def show_panel_at_pet() -> None:
        """Show the message bubble right above the pet's head (click to expand)."""
        panel.position_near(engine.root_x, pet_head_y())
        panel.show_bubble()

    integration.on_activity = show_panel_at_pet
    window.set_dsh_integration(integration)
    integration.start()

    primary = QGuiApplication.primaryScreen()
    available = primary.availableGeometry()
    engine.place_at(available.center().x(), float(available.bottom()))
    window.move(
        int(round(engine.root_x - engine.pivot_display_x)),
        int(round(engine.root_y - engine.pivot_display_y)),
    )
    window.show()

    ticker = QTimer()
    ticker.setTimerType(Qt.TimerType.PreciseTimer)
    ticker.setInterval(TICK_INTERVAL_MS)
    last_tick = [time.perf_counter()]
    last_cursor = [QCursor.pos()]

    def on_tick() -> None:
        now = time.perf_counter()
        dt = min(MAX_DT_S, max(0.0, now - last_tick[0]))
        last_tick[0] = now
        cursor = QCursor.pos()
        # Track mouse activity for scenario triggers (idle durations).
        if cursor != last_cursor[0]:
            last_cursor[0] = cursor
            engine.note_input(now)
        ground = ground_y_at(engine.root_x, engine.root_y) if engine.states.state is PetState.FALLING else None
        x_bounds = None
        if engine.states.state is PetState.FALLING:
            bounds = desktop_bounds()
            if bounds is not None:
                x_bounds = (
                    bounds.x + engine.pivot_display_x,
                    bounds.right - (engine.display_w - engine.pivot_display_x),
                )
        frame = engine.tick(now, dt, (float(cursor.x()), float(cursor.y())), ground, x_bounds)
        integration.maintain_anim()
        # Keep the message bar pinned above the pet's head while visible.
        if panel.isVisible():
            panel.position_near(engine.root_x, pet_head_y())
        snapshot = build_snapshot(frame, engine, (engine.display_w, engine.display_h))
        pixmap = renderer_holder["renderer"].render(snapshot)
        window.show_frame(snapshot, pixmap)
        window.move(
            int(round(engine.root_x - engine.pivot_display_x)),
            int(round(engine.root_y - engine.pivot_display_y)),
        )

    check = QTimer()
    check.setInterval(int(config["scheduler"]["idle_check_interval_s"] * 1000))

    dsh_poll = QTimer()
    dsh_poll.setInterval(int(DshIntegration.POLL_INTERVAL_S * 1000))
    dsh_drain = QTimer()
    dsh_drain.setInterval(500)

    def on_check() -> None:
        now = time.perf_counter()
        if engine.scenario_check(now):
            return
        engine.try_random_performance(now)

    def on_dsh_poll() -> None:
        integration.poll_status_async()

    def on_dsh_drain() -> None:
        integration.drain_events()
        integration.drain_poll()

    panel_idle = QTimer()
    panel_idle.setInterval(5000)

    def on_panel_idle() -> None:
        # Auto-hide the message bar after 90 s of DSH silence while idle.
        if (
            panel.isVisible()
            and not integration.working
            and time.time() - integration.last_activity_at > 90.0
        ):
            panel.hide()

    check.timeout.connect(on_check)
    dsh_poll.timeout.connect(on_dsh_poll)
    dsh_drain.timeout.connect(on_dsh_drain)
    panel_idle.timeout.connect(on_panel_idle)

    def shutdown() -> None:
        ticker.stop()
        check.stop()
        dsh_poll.stop()
        dsh_drain.stop()
        panel_idle.stop()
        integration.stop()

    window.closed.connect(shutdown)
    window.closed.connect(app.quit)
    app.aboutToQuit.connect(shutdown)

    ticker.timeout.connect(on_tick)
    ticker.start()
    check.start()
    dsh_poll.start()
    dsh_drain.start()
    panel_idle.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
