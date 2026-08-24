"""Collision geometry tests (pure Python, no Qt needed)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.collision import WorkArea, ground_for_point, step_fall  # noqa: E402


class GroundSelectionTests(unittest.TestCase):
    def test_multi_monitor_virtual_desktop_without_origin_assumption(self) -> None:
        # Primary screen does not start at (0, 0): a second monitor sits to
        # the left on the virtual desktop.
        left = WorkArea(-1920, 0, 1920, 1040)
        primary = WorkArea(0, 0, 1920, 1040)
        self.assertEqual(ground_for_point((left, primary), -500, 900).bottom, 1040)
        self.assertEqual(ground_for_point((left, primary), 800, 900).bottom, 1040)
        # A point between monitors falls back to the nearest area.
        between = ground_for_point((left, primary), -10, 500)
        self.assertIsNotNone(between)
        # Empty desktop -> no ground.
        self.assertIsNone(ground_for_point((), 10, 10))

    def test_taller_taskbar_shrinks_only_that_monitors_area(self) -> None:
        tall = WorkArea(0, 0, 1920, 1000)  # taskbar eats 40 px on this monitor
        short = WorkArea(1920, 0, 1920, 1040)
        self.assertEqual(ground_for_point((tall, short), 100, 999).bottom, 1000)
        self.assertEqual(ground_for_point((tall, short), 2000, 999).bottom, 1040)


class RootNodeCollisionTests(unittest.TestCase):
    def test_collision_uses_root_node_not_transparent_frame_bbox(self) -> None:
        # Work area bottom is 800. The character's feet root is at 795, but
        # the transparent PNG frame extends 24 px below the feet (its bbox
        # bottom would be 819). Collision must only test the root node.
        root_y = 795.0
        vy = 0.0
        hit = False
        for _ in range(10):
            step = step_fall(100.0, root_y, 0.0, vy, 0.02, 2000.0, ground_y=800.0)
            root_y, vy, hit = step.root_y, step.vy, step.hit
            if hit:
                break
        self.assertTrue(hit, "falling far enough must eventually collide")
        self.assertEqual(root_y, 800.0)
        self.assertEqual(vy, 0.0)

        # And crucially: at the moment before contact the root was still
        # above the ground although the frame bbox was already below it.
        self.assertLess(step.root_y - 3.2, 800.0)  # previous integration step
        self.assertGreater(795.0 + 24.0, 800.0, "frame bbox hangs below the work area")

    def test_falling_velocity_and_clamp(self) -> None:
        step = step_fall(10.0, 100.0, -120.0, 30.0, 0.016, 2000.0, air_drag=0.8, max_speed=2800.0)
        self.assertAlmostEqual(step.vy, 62.0, delta=1.0)
        self.assertGreater(step.vx, -120.0)  # air drag slows horizontal motion
        self.assertLess(step.vx, 0.0)

    def test_no_ground_means_no_collision(self) -> None:
        step = step_fall(0.0, 1000.0, 0.0, 100.0, 1.0, 2000.0, ground_y=None)
        self.assertFalse(step.hit)


if __name__ == "__main__":
    unittest.main()
