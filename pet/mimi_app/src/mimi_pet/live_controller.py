from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveConfig:
    breath_period_s: float = 3.8
    breath_amplitude: float = 0.65
    gaze_smoothing: float = 0.18
    max_head_angle_deg: float = 4.0


@dataclass(frozen=True)
class LiveParameters:
    body_breathe: float
    head_angle: float
    head_x: float
    head_y: float


class LiveController:
    def __init__(self, config: LiveConfig | None = None) -> None:
        self.config = config or LiveConfig()
        self._head_x = 0.0
        self._head_y = 0.0

    def update(
        self,
        timestamp_s: float,
        cursor_dx: float,
        cursor_dy: float,
        dragging: bool = False,
        performing: bool = False,
        enabled: bool = True,
    ) -> LiveParameters:
        influence = 0.2 if performing else 1.0
        if not enabled:
            # Falling / Landing: no breathing and no cursor gaze at all.
            influence = 0.0
        target_x = max(-9.0, min(9.0, cursor_dx / 45.0)) * influence
        target_y = max(-5.0, min(7.0, cursor_dy / 55.0)) * influence
        alpha = self.config.gaze_smoothing
        self._head_x += alpha * (target_x - self._head_x)
        self._head_y += alpha * (target_y - self._head_y)
        angle = max(
            -self.config.max_head_angle_deg,
            min(self.config.max_head_angle_deg, self._head_x * 0.44),
        )
        amplitude = self.config.breath_amplitude * (0.3 if dragging else 1.0)
        if not enabled:
            amplitude = 0.0
        breathe = math.sin(timestamp_s * math.tau / self.config.breath_period_s) * amplitude
        return LiveParameters(breathe, angle, self._head_x, self._head_y)
