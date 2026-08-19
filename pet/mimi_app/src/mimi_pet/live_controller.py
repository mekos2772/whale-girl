from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveConfig:
    breath_period_s: float = 3.8
    breath_amplitude: float = 0.65
    gaze_smoothing: float = 0.18
    max_head_angle_deg: float = 4.0
    # Ambient idle head sway ("摇头晃脑", Live-like standing bob): two
    # detuned sine terms so the motion reads organic, not a metronome.
    idle_sway_period_s: float = 3.2
    idle_sway_period2_s: float = 7.7
    idle_sway_amplitude_deg: float = 3.0
    idle_bob_period_s: float = 4.4
    idle_bob_amplitude: float = 1.6
    # How fast the sway fades in when the character is free and out when it
    # is held / airborne / landing, so entering idle never pops.
    sway_fade_rate: float = 6.0


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
        self._sway_fade = 0.0

    def update(
        self,
        timestamp_s: float,
        cursor_dx: float,
        cursor_dy: float,
        dragging: bool = False,
        performing: bool = False,
        enabled: bool = True,
        dt_s: float = 0.0,
    ) -> LiveParameters:
        influence = 0.2 if performing else 1.0
        if not enabled:
            # Falling / Landing: no breathing, no cursor gaze, no sway.
            influence = 0.0
        target_x = max(-9.0, min(9.0, cursor_dx / 45.0)) * influence
        target_y = max(-5.0, min(7.0, cursor_dy / 55.0)) * influence
        alpha = self.config.gaze_smoothing
        self._head_x += alpha * (target_x - self._head_x)
        self._head_y += alpha * (target_y - self._head_y)
        gaze_angle = max(
            -self.config.max_head_angle_deg,
            min(self.config.max_head_angle_deg, self._head_x * 0.44),
        )
        amplitude = self.config.breath_amplitude * (0.3 if dragging else 1.0)
        if not enabled:
            amplitude = 0.0
        breathe = math.sin(timestamp_s * math.tau / self.config.breath_period_s) * amplitude

        # Idle head sway ("摇头" rotation + subtle "点头" bob). Enabled while
        # free-standing; off while held (drag), airborne or landing. Scaled by
        # `influence` so running performances only keep a subtle hint of life.
        sway_target = 1.0 if (enabled and not dragging) else 0.0
        if dt_s > 0.0:
            rate = self.config.sway_fade_rate
            self._sway_fade += (sway_target - self._sway_fade) * min(1.0, rate * dt_s)
            self._sway_fade = max(0.0, min(1.0, self._sway_fade))
        else:
            self._sway_fade = sway_target
        t = timestamp_s * math.tau
        fast = math.sin(t / self.config.idle_sway_period_s)
        slow = math.sin(t / self.config.idle_sway_period2_s)
        sway = (fast * 0.72 + slow * 0.28) * self.config.idle_sway_amplitude_deg
        sway *= self._sway_fade * influence
        bob = math.sin(t / self.config.idle_bob_period_s) * self.config.idle_bob_amplitude
        bob *= self._sway_fade * influence

        # Gaze turn keeps its own headroom; sway sits on top but the total
        # stays under a sane bound so it never reads as a convulsion.
        bound = self.config.max_head_angle_deg + self.config.idle_sway_amplitude_deg
        angle = max(-bound, min(bound, gaze_angle + sway))
        head_y = max(-5.0, min(7.0, self._head_y + bob))
        return LiveParameters(breathe, angle, self._head_x, head_y)
