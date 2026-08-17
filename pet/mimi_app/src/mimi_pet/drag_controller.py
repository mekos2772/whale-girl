"""Hybrid drag controller for dense, directly drawn speed-force poses.

The v13 art contains eleven force points per horizontal direction.  Smoothed
pointer speed chooses t00..t10, but selection advances by at most one point per
update so a sudden mouse acceleration cannot skip all intermediate drawings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DragConfig:
    velocity_smoothing: float = 0.28
    dead_zone_px_s: float = 90.0
    medium_enter_px_s: float = 260.0
    medium_exit_px_s: float = 200.0
    fast_enter_px_s: float = 950.0
    fast_exit_px_s: float = 800.0
    horizontal_bias: float = 1.15
    micro_max_angle_deg: float = 0.35
    pose_count: int = 11
    spring_strength: float = 0.22
    spring_damping: float = 0.72
    long_press_s: float = 2.0
    drag_start_move_px: float = 8.0


@dataclass(frozen=True)
class DragOutput:
    direction: str
    speed_band: str  # hold | slow | medium | fast
    pose_set: str
    velocity_x: float
    velocity_y: float
    speed: float
    root_drag_x: float
    root_drag_y: float
    body_drag_angle: float
    hair_drag_lag: float
    skirt_drag_lag: float
    drag_speed: float
    pose_index: int


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class DragHybridController:
    """Converts pointer samples into stable pose selection and Live parameters."""

    def __init__(self, config: DragConfig | None = None) -> None:
        self.config = config or DragConfig()
        self._last_x: float | None = None
        self._last_y: float | None = None
        self._last_t: float | None = None
        self._vx = 0.0
        self._vy = 0.0
        self._direction = "right"
        self._speed_band = "hold"
        self._pose_index = 0
        self._spring_x = 0.0
        self._spring_y = 0.0
        self._spring_vx = 0.0
        self._spring_vy = 0.0

    def reset(self, x: float, y: float, timestamp_s: float) -> None:
        self._last_x, self._last_y, self._last_t = x, y, timestamp_s
        self._vx = self._vy = 0.0
        self._speed_band = "hold"
        self._pose_index = 0
        self._spring_x = self._spring_y = 0.0
        self._spring_vx = self._spring_vy = 0.0

    def update(self, x: float, y: float, timestamp_s: float) -> DragOutput:
        if self._last_t is None:
            self.reset(x, y, timestamp_s)
            return self._output()
        dt = max(1 / 240, min(0.1, timestamp_s - self._last_t))
        raw_vx = (x - float(self._last_x)) / dt
        raw_vy = (y - float(self._last_y)) / dt
        alpha = _clamp(self.config.velocity_smoothing, 0.01, 1.0)
        self._vx += alpha * (raw_vx - self._vx)
        self._vy += alpha * (raw_vy - self._vy)
        self._last_x, self._last_y, self._last_t = x, y, timestamp_s

        speed = math.hypot(self._vx, self._vy)
        if speed >= self.config.dead_zone_px_s:
            if abs(self._vx) > abs(self._vy) * self.config.horizontal_bias:
                self._direction = "right" if self._vx > 0 else "left"
            elif abs(self._vy) > abs(self._vx) * self.config.horizontal_bias:
                self._direction = "down" if self._vy > 0 else "up"
            # Near diagonal keeps the previous direction to prevent chatter.

        # Three speed tiers with hysteresis (enter 260/950, exit 200/800).
        band = self._speed_band
        if band == "fast":
            band = "fast" if speed >= self.config.fast_exit_px_s else "medium"
        elif band == "medium":
            if speed >= self.config.fast_enter_px_s:
                band = "fast"
            elif speed < self.config.medium_exit_px_s:
                band = "slow"
        else:  # hold or slow
            if speed >= self.config.fast_enter_px_s:
                band = "fast"
            elif speed >= self.config.medium_enter_px_s:
                band = "medium"
            elif speed >= self.config.dead_zone_px_s:
                band = "slow"
            else:
                band = "hold"
        self._speed_band = band

        if band == "hold":
            # No authored rebound/set-down frame: settle toward the neutral
            # hanging point through the same adjacent dense poses.
            if self._pose_index > 0:
                self._pose_index -= 1
        else:
            pose_count = max(2, int(self.config.pose_count))
            speed_span = max(self.config.fast_enter_px_s - self.config.dead_zone_px_s, 1.0)
            force = _clamp((speed - self.config.dead_zone_px_s) / speed_span, 0.0, 1.0)
            target_index = int(round(force * (pose_count - 1)))
            if target_index > self._pose_index:
                self._pose_index += 1
            elif target_index < self._pose_index:
                self._pose_index -= 1

        target_spring_x = _clamp(-self._vx * 0.015, -18.0, 18.0)
        target_spring_y = _clamp(-self._vy * 0.012, -14.0, 14.0)
        strength = _clamp(self.config.spring_strength, 0.01, 1.0)
        damping = _clamp(self.config.spring_damping, 0.0, 0.99)
        self._spring_vx = (self._spring_vx + (target_spring_x - self._spring_x) * strength) * damping
        self._spring_vy = (self._spring_vy + (target_spring_y - self._spring_y) * strength) * damping
        self._spring_x = _clamp(self._spring_x + self._spring_vx, -18.0, 18.0)
        self._spring_y = _clamp(self._spring_y + self._spring_vy, -14.0, 14.0)
        return self._output()

    def _output(self) -> DragOutput:
        speed = math.hypot(self._vx, self._vy)
        normalized = _clamp(speed / self.config.fast_enter_px_s, 0.0, 1.0)
        # The dense pose art carries almost all tilt; this tiny residual only
        # feeds the rig's hair/skirt lag and cannot visibly stretch the bitmap.
        micro = self.config.micro_max_angle_deg
        angle = _clamp(-self._vx / self.config.fast_enter_px_s * micro, -micro, micro)
        pose_direction = self._direction if self._direction in {"left", "right"} else "nearest_horizontal"
        return DragOutput(
            direction=self._direction,
            speed_band=self._speed_band,
            pose_set=f"drag_{pose_direction}_t{self._pose_index:02d}",
            velocity_x=self._vx,
            velocity_y=self._vy,
            speed=speed,
            root_drag_x=self._spring_x,
            root_drag_y=self._spring_y,
            body_drag_angle=angle,
            hair_drag_lag=_clamp(angle * 0.85, -micro, micro),
            skirt_drag_lag=_clamp(angle * 0.58, -micro, micro),
            drag_speed=normalized,
            pose_index=self._pose_index,
        )
