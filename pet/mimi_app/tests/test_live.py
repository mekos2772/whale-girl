"""LiveController idle sway ("摇头晃脑") tests.

The sway must fade in smoothly, stay inside the sane angle bound, run
organically (both signs over time), and switch off while dragging, airborne,
landing or (scaled down) during performances.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.live_controller import LiveConfig, LiveController  # noqa: E402

DT = 1.0 / 60.0


def run(controller: LiveController, seconds: float, **kwargs) -> list:
    """Drive the controller and return the LiveParameters history."""
    samples: list = []
    t = 0.0
    steps = int(seconds / DT)
    for _ in range(steps):
        t += DT
        samples.append(controller.update(t, 0.0, 0.0, dt_s=DT, **kwargs))
    return samples


class LiveIdleSwayTests(unittest.TestCase):
    def test_sway_starts_flat_grows_in_and_stays_in_bounds(self) -> None:
        ctrl = LiveController(LiveConfig(max_head_angle_deg=4.0, idle_sway_amplitude_deg=2.0))
        samples = run(ctrl, 15.0)
        # No pop at enable: the very first frame is a fraction of a degree,
        # not a jump to the 2 deg amplitude.
        self.assertLess(samples[0].head_angle, 0.02, "no pop at enable")
        bound = 4.0 + 2.0
        for sample in samples:
            self.assertLessEqual(abs(sample.head_angle), bound + 1e-9)
        # After fade-in the two-toned sine must genuinely swing both ways.
        self.assertTrue(any(s.head_angle > 1.0 for s in samples[60:]))
        self.assertTrue(any(s.head_angle < -1.0 for s in samples[60:]))

    def test_bob_translates_head_y_subtly(self) -> None:
        ctrl = LiveController(LiveConfig(idle_bob_amplitude=1.2))
        samples = run(ctrl, 15.0)
        for sample in samples:
            self.assertLessEqual(abs(sample.head_y), 1.2 + 1e-9)
        self.assertTrue(any(abs(s.head_y) > 0.5 for s in samples[60:]))

    def test_sway_is_off_while_dragging(self) -> None:
        ctrl = LiveController(LiveConfig(idle_sway_amplitude_deg=2.0))
        samples = run(ctrl, 4.0, dragging=True)
        for sample in samples:
            self.assertEqual(sample.head_angle, 0.0)
            self.assertAlmostEqual(sample.head_y, 0.0, delta=1e-9)
        # Breathing is kept but halved while held.
        self.assertTrue(any(s.body_breathe != 0.0 for s in samples))

    def test_sway_scaled_down_during_performance(self) -> None:
        performing = LiveController(LiveConfig(idle_sway_amplitude_deg=2.0))
        p_samples = run(performing, 8.0, performing=True)
        for sample in p_samples:
            self.assertLess(abs(sample.head_angle), 0.5, "performance sway is only a hint")
        free = LiveController(LiveConfig(idle_sway_amplitude_deg=2.0))
        f_samples = run(free, 8.0)
        peak_free = max(abs(s.head_angle) for s in f_samples[60:])
        peak_perf = max(abs(s.head_angle) for s in p_samples[60:])
        self.assertGreater(peak_free, peak_perf)

    def test_sway_and_breathing_off_when_disabled(self) -> None:
        ctrl = LiveController(LiveConfig(idle_sway_amplitude_deg=2.0))
        samples = run(ctrl, 4.0, enabled=False)
        for sample in samples:
            self.assertEqual(sample.head_angle, 0.0)
            self.assertAlmostEqual(sample.head_y, 0.0, delta=1e-6)
            self.assertEqual(sample.body_breathe, 0.0)


if __name__ == "__main__":
    unittest.main()
