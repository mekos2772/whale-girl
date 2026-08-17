"""Rendering: RenderSnapshot plus the Qt compositor for full frames and the
layered rig.

``RenderSnapshot`` is plain data (no Qt types) so the engine layer never
depends on Qt. Only ``QtRenderer`` touches QPixmap/QPainter and it is used
exclusively by the GUI adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform

from .engine import EngineFrame, PetEngine
from .frame_player import FrameSample
from .image_cache import ImageCache
from .rig_model import RigModel
from .state_machine import PetState


@dataclass(frozen=True)
class RenderSnapshot:
    mode: str  # "full_frame" | "rig" | "drag_pose"
    frame_path: Path | None
    expression: str
    state: str
    action_id: str | None
    frame_index: int | None
    frame_total: int | None
    root_x: float
    root_y: float
    vx: float
    vy: float
    grab_dx: float
    grab_dy: float
    direction: str
    speed_band: str
    pose_set: str
    drag_speed: float
    body_tilt: float
    hair_lag: float
    skirt_lag: float
    display_size: tuple[int, int]
    pivot_display: tuple[float, float]
    ground_y: float | None
    # Live layer parameters (None when rendering a full frame).
    body_breathe: float = 0.0
    head_angle: float = 0.0
    head_x: float = 0.0
    head_y: float = 0.0
    root_drag_x: float = 0.0
    root_drag_y: float = 0.0
    # Hybrid drag pose loop info.
    drag_pose_set: str | None = None
    drag_pose_index: int | None = None
    # One-time fixed registration scale (never per-frame alpha fit).
    registration_scale: float = 1.0
    registration_scale_y: float = 1.0
    bubble_text: str | None = None
    click_react: float = 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_snapshot(frame: EngineFrame, engine: PetEngine, display_size: tuple[int, int]) -> RenderSnapshot:
    """Turn an engine frame into pure render data; never mutates the engine."""
    full_frame = frame.sprite is not None and frame.state in (PetState.PERFORMING, PetState.LANDING)
    drag_pose = frame.state is PetState.DRAGGING and frame.drag_pose_path is not None
    drag = frame.drag if frame.state is PetState.DRAGGING else None
    if frame.state is PetState.FALLING:
        tilt = _clamp(-frame.vx / 1000.0 * 12.0, -12.0, 12.0)
        hair_lag = _clamp(tilt * 0.85, -10.0, 10.0)
        skirt_lag = _clamp(tilt * 0.58, -7.0, 7.0)
    elif drag is not None:
        tilt = drag.body_drag_angle
        hair_lag = drag.hair_drag_lag
        skirt_lag = drag.skirt_drag_lag
    else:
        tilt = hair_lag = skirt_lag = 0.0
    total = len(engine.player.action.frames) if engine.player.action else None
    if full_frame:
        mode, path, index = "full_frame", frame.sprite.path, frame.sprite.index
        action = engine.player.action
        registration_scale = action.registration_scale if action else 1.0
        registration_scale_y = action.registration_scale_y if action else 1.0
    elif drag_pose:
        mode, path, index = "drag_pose", frame.drag_pose_path, frame.drag_pose_index
        action = engine.drag_pose_player.action
        registration_scale = action.registration_scale if action else 1.0
        registration_scale_y = action.registration_scale_y if action else 1.0
    else:
        mode, path, index = "rig", None, None
        registration_scale = registration_scale_y = 1.0
    return RenderSnapshot(
        mode=mode,
        frame_path=path,
        expression=frame.expression,
        state=frame.state.name,
        action_id=frame.action_id,
        frame_index=index,
        frame_total=total,
        root_x=frame.root_x,
        root_y=frame.root_y,
        vx=frame.vx,
        vy=frame.vy,
        grab_dx=frame.grab_dx,
        grab_dy=frame.grab_dy,
        direction=drag.direction if drag else "",
        speed_band=drag.speed_band if drag else "",
        pose_set=drag.pose_set if drag else "",
        drag_speed=drag.drag_speed if drag else 0.0,
        body_tilt=tilt,
        hair_lag=hair_lag,
        skirt_lag=skirt_lag,
        display_size=display_size,
        pivot_display=(engine.pivot_display_x, engine.pivot_display_y),
        ground_y=frame.ground_y,
        body_breathe=frame.live.body_breathe if frame.live else 0.0,
        head_angle=frame.live.head_angle if frame.live else 0.0,
        head_x=frame.live.head_x if frame.live else 0.0,
        head_y=frame.live.head_y if frame.live else 0.0,
        root_drag_x=drag.root_drag_x if drag else 0.0,
        root_drag_y=drag.root_drag_y if drag else 0.0,
        drag_pose_set=frame.drag_pose_set,
        drag_pose_index=index if drag_pose else None,
        registration_scale=registration_scale,
        registration_scale_y=registration_scale_y,
        bubble_text=frame.bubble_text,
        click_react=frame.click_react,
    )


class QtRenderer:
    """Composes a RenderSnapshot into a QPixmap at the display size.

    The rig is composed with the authored model.json transforms and then
    registered onto the full-frame ground root with ONE fixed calibration
    (read from config["rig"], computed once by tools/calibrate_rig_root.py).
    Nothing here is recomputed from alpha bounding boxes per frame.
    """

    def __init__(
        self,
        cache: ImageCache,
        rig: RigModel,
        display_size: tuple[int, int],
        calibration: dict | None = None,
        gaze_translation_scale: float = 0.35,
        gaze_hair_counter_scale: float = 0.3,
    ) -> None:
        self.cache = cache
        self.rig = rig
        self.display_w, self.display_h = display_size
        calibration = calibration or {}
        master_scale = float(calibration.get("master_scale", 1.0))
        master_offset_x = float(calibration.get("master_offset_x", 0.0))
        master_offset_y = float(calibration.get("master_offset_y", 0.0))
        # Cursor gaze combines independent iris motion with a small neck turn.
        # Head translation remains heavily damped so the face never drifts.
        self._gaze_translation_scale = gaze_translation_scale
        self._gaze_hair_counter_scale = gaze_hair_counter_scale
        # Layer images are scaled from the 1024x1536 master to the window.
        self._px_scale = self.display_w / rig.canvas[0]
        # Fixed registration of the composed rig onto the full-frame ground
        # root (see tools/calibrate_rig_root.py). Applied once per layer as:
        #   pivot_s = pivot * _px_scale            (rotation centre)
        #   layer_scale = base_scale * master_scale
        #   translate = (pivot + offset) * master_scale * _px_scale + _ty_disp
        self._master_scale = master_scale
        self._tx_disp = master_offset_x * self._px_scale
        self._ty_disp = master_offset_y * self._px_scale
        # Pickup anchor near the collar: derived from the head pivot (chin/
        # neck area) after calibration. Body drag tilt rotates around this
        # point so the character swings from its collar like being carried,
        # never like a pendulum rocking on its feet.
        try:
            head_pivot = rig.layer("head_expression").pivot[1]
        except KeyError:
            head_pivot = 654.0
        self._pickup_y = head_pivot * master_scale * self._px_scale + self._ty_disp
        self._layer_pixmaps: dict[str, QPixmap] = {}
        self._head_pixmaps: dict[tuple[float, float], QPixmap] = {}

    # ------------------------------------------------------------------ API

    def render(self, snapshot: RenderSnapshot) -> QPixmap:
        if snapshot.mode == "drag_pose" and snapshot.frame_path is not None:
            return self._render_drag_pose(snapshot)
        if snapshot.mode == "full_frame" and snapshot.frame_path is not None:
            return self._render_full_frame(snapshot)
        return self._render_rig(snapshot)

    def _render_full_frame(self, snapshot: RenderSnapshot) -> QPixmap:
        """Full frame with its ONE-TIME fixed registration scale applied.

        The scale is authored in the manifest (e.g. land_recover was
        generated ~9% larger than the rest) and applied about the ground
        pivot; nothing here is recomputed from alpha bounding boxes.
        """
        pixmap = self.cache.scaled_pixmap(snapshot.frame_path, QSize(self.display_w, self.display_h))
        scale_x = snapshot.registration_scale
        scale_y = snapshot.registration_scale_y
        if abs(scale_x - 1.0) < 1e-6 and abs(scale_y - 1.0) < 1e-6:
            return pixmap
        pivot_x, pivot_y = snapshot.pivot_display
        canvas = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        transform = QTransform()
        transform.translate(pivot_x, pivot_y)
        transform.scale(scale_x, scale_y)
        transform.translate(-pivot_x, -pivot_y)
        painter.setTransform(transform)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return QPixmap.fromImage(canvas)

    def _render_drag_pose(self, snapshot: RenderSnapshot) -> QPixmap:
        """Hybrid drag: the approved pose frame plus program micro-tune.

        v13 frames already share one fixed top pickup anchor. The tiny residual
        rotation is applied around that same point; frames are never fitted.
        """
        pixmap = self.cache.scaled_pixmap(snapshot.frame_path, QSize(self.display_w, self.display_h))
        scale_x = snapshot.registration_scale
        scale_y = snapshot.registration_scale_y
        if (
            abs(scale_x - 1.0) < 1e-6
            and abs(scale_y - 1.0) < 1e-6
            and not snapshot.body_tilt
            and not snapshot.root_drag_x
        ):
            return pixmap
        canvas = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pickup_x = 256.0 * self.display_w / 512.0
        pickup_y = 80.0 * self.display_h / 768.0
        transform = QTransform()
        transform.translate(pickup_x, pickup_y)
        transform.scale(scale_x, scale_y)
        transform.rotate(snapshot.body_tilt)
        transform.translate(-pickup_x, -pickup_y)
        transform.translate(snapshot.root_drag_x, 0.0)
        painter.setTransform(transform)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return QPixmap.fromImage(canvas)

    def _layer_pixmap(self, layer_name: str, file: Path) -> QPixmap:
        key = f"{layer_name}:{file}"
        pixmap = self._layer_pixmaps.get(key)
        if pixmap is None:
            pixmap = self.cache.scaled_pixmap(file, QSize(self.display_w, self.display_h))
            self._layer_pixmaps[key] = pixmap
        return pixmap

    # ------------------------------------------------------------------ rig

    def _render_rig(self, snapshot: RenderSnapshot) -> QPixmap:
        canvas = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        breathe = snapshot.body_breathe
        body_tilt = snapshot.body_tilt
        ground_x, ground_y = snapshot.pivot_display
        # Drag/fall tilt swings the character around the collar pickup anchor
        # (fixed height), not around the feet.
        tilt_center_y = self._pickup_y if body_tilt else ground_y
        breathe_scale = 1.0 + breathe * 0.006
        # Click reaction: a soft vertical squash and rebound (0..1 pulse).
        click_squash = 1.0 - snapshot.click_react * 0.05
        head_file = self._head_file(snapshot.expression)

        for layer in self.rig.layers:
            if layer.experimental:
                # skirt_hem is an experimental front-skirt layer with NO
                # calibration in fit_report.json and no placement in the
                # official rig preview script. Compositing it uncalibrated
                # draws a second, misaligned skirt in front of the body.
                # Keep its parameters reserved but never render it.
                continue
            self._draw_layer(
                painter,
                layer,
                snapshot,
                ground_x,
                tilt_center_y,
                breathe,
                breathe_scale,
                body_tilt,
                head_file,
                click_squash=click_squash,
            )
        painter.end()

        pixmap = QPixmap.fromImage(canvas)
        # Root spring offset: the character sways inside the fixed window.
        # The vertical spring is suppressed while dragging: without vertical
        # drag assets the character must keep one fixed height (no crouch
        # bobbing from mouse Y jitter).
        root_drag_y = 0.0 if snapshot.state == "DRAGGING" else snapshot.root_drag_y
        if snapshot.root_drag_x or root_drag_y:
            shifted = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
            shifted.fill(Qt.GlobalColor.transparent)
            p2 = QPainter(shifted)
            p2.drawImage(QPointF(snapshot.root_drag_x, root_drag_y), canvas)
            p2.end()
            pixmap = QPixmap.fromImage(shifted)
        return pixmap

    def _head_file(self, expression: str) -> Path:
        head = self.rig.layer("head_expression")
        filename = self.rig.expressions.get(expression) or self.rig.expressions.get("neutral")
        if filename is None or filename not in head.switches:
            filename = head.switches[0] if head.switches else head.file.name
        return head.file.parent / filename

    @staticmethod
    def _normalised_parameter(value: float, limits: dict[str, float] | None) -> float:
        if not limits:
            return _clamp(value, -1.0, 1.0)
        default = float(limits.get("default", 0.0))
        relative = value - default
        span = float(limits.get("max" if relative >= 0 else "min", 1.0)) - default
        if not span:
            return 0.0
        return _clamp(relative / abs(span), -1.0, 1.0)

    def _head_pixmap(self, snapshot: RenderSnapshot, head_file: Path) -> QPixmap:
        """Return the reviewed whole face or a clipped neutral eye composite."""
        tracking = self.rig.eye_tracking
        if tracking is None or snapshot.expression not in tracking.supported_expressions:
            return self._layer_pixmap("head_expression", head_file)

        x_name, y_name = tracking.source_parameters
        eye_x = self._normalised_parameter(snapshot.head_x, self.rig.parameters.get(x_name))
        eye_y = self._normalised_parameter(snapshot.head_y, self.rig.parameters.get(y_name))
        dx = eye_x * tracking.max_offset[0] * self._px_scale
        dy = eye_y * tracking.max_offset[1] * self._px_scale

        # Quarter-pixel quantisation prevents cache churn while staying much
        # smoother than the one-pixel jumps visible at the 512px runtime size.
        dx = round(dx * 4.0) / 4.0
        dy = round(dy * 4.0) / 4.0
        if abs(dx) < 0.125 and abs(dy) < 0.125:
            # Exact approved master at rest: no compositing differences and no
            # possibility of a one-frame pop when auto-blink starts.
            return self._layer_pixmap("head_expression", head_file)
        key = (dx, dy)
        cached = self._head_pixmaps.get(key)
        if cached is not None:
            return cached

        base = self._layer_pixmap("eye_base", tracking.base_file)
        irises = self._layer_pixmap("eye_irises", tracking.irises_file)
        clip = self._layer_pixmap("eye_clip", tracking.clip_file)
        foreground = self._layer_pixmap("eye_foreground", tracking.foreground_file)

        moved = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
        moved.fill(Qt.GlobalColor.transparent)
        eye_painter = QPainter(moved)
        eye_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        eye_painter.drawPixmap(QPointF(dx, dy), irises)
        eye_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        eye_painter.drawPixmap(0, 0, clip)
        eye_painter.end()

        composed = QImage(self.display_w, self.display_h, QImage.Format.Format_ARGB32_Premultiplied)
        composed.fill(Qt.GlobalColor.transparent)
        painter = QPainter(composed)
        painter.drawPixmap(0, 0, base)
        painter.drawImage(0, 0, moved)
        painter.drawPixmap(0, 0, foreground)
        painter.end()
        result = QPixmap.fromImage(composed)
        self._head_pixmaps[key] = result
        return result

    def _draw_layer(
        self,
        painter: QPainter,
        layer,
        snapshot: RenderSnapshot,
        ground_x: float,
        ground_y: float,
        breathe: float,
        breathe_scale: float,
        body_tilt: float,
        head_file: Path,
        click_squash: float = 1.0,
    ) -> None:
        file = head_file if layer.name == "head_expression" else layer.file
        pixmap = (
            self._head_pixmap(snapshot, head_file)
            if layer.name == "head_expression"
            else self._layer_pixmap(layer.name, file)
        )
        # Rotation centre in display px (layer image is the master scaled to
        # the window).
        px = layer.pivot[0] * self._px_scale
        py = layer.pivot[1] * self._px_scale
        # Fixed calibration: layer scale = authored base_scale * master_scale;
        # translate = (pivot + authored offset) * master_scale * px_scale.
        layer_scale = layer.base_scale * self._master_scale
        tx = (layer.pivot[0] + layer.base_offset[0]) * self._master_scale * self._px_scale + self._tx_disp
        ty = (layer.pivot[1] + layer.base_offset[1]) * self._master_scale * self._px_scale + self._ty_disp

        extra_x = extra_y = 0.0
        local_angle = 0.0
        scale_x = layer_scale
        scale_y = layer_scale
        if layer.name == "head_expression":
            # Damped translation (micro-shift) + rotation about the neck-side
            # pivot: reads as a subtle turn, not a drifting head.
            extra_x = snapshot.head_x * self._gaze_translation_scale
            extra_y = snapshot.head_y * self._gaze_translation_scale
            local_angle = snapshot.head_angle
        elif layer.name == "body_base":
            scale_y = layer_scale * breathe_scale * click_squash
            # Keep the feet from drifting while breathing: compensate the
            # scale about the body pivot.
            extra_y = breathe * 0.006 * (ground_y - py)
            extra_y += (click_squash - 1.0) * (ground_y - py)
        elif layer.name in ("arm_left", "arm_right"):
            local_angle = breathe * 0.18 * 5.0
        elif layer.name == "back_hair":
            # Back hair lags the head turn slightly (counter-rotation).
            local_angle = snapshot.hair_lag - snapshot.head_angle * self._gaze_hair_counter_scale
        elif layer.name == "skirt_hem":
            local_angle = snapshot.skirt_lag + breathe * 0.15 * 2.0

        transform = QTransform()
        transform.translate(ground_x, ground_y)
        transform.rotate(body_tilt)
        transform.translate(-ground_x, -ground_y)
        transform.translate(tx + extra_x, ty + extra_y)
        transform.rotate(local_angle)
        transform.scale(scale_x, scale_y)
        transform.translate(-px, -py)
        painter.setTransform(transform)
        painter.drawPixmap(0, 0, pixmap)
