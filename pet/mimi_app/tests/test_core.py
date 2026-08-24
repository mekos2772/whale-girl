from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.drag_controller import DragConfig, DragHybridController  # noqa: E402
from mimi_pet.frame_player import FramePlayer  # noqa: E402
from mimi_pet.state_machine import Event, PetState, PetStateMachine  # noqa: E402


class AssetLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.library = ActionLibrary(
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json"
        )

    def test_only_approved_full_frame_actions_are_enabled(self) -> None:
        ids = {action.id for action in self.library.all()}
        self.assertEqual(len(ids), 23)
        self.assertIn("land_recover_v4_12", ids)
        self.assertIn("harness_task_thinking_v1_12", ids)
        self.assertIn("harness_tool_working_v1_12", ids)
        self.assertIn("walk_left", ids)
        self.assertIn("sleep_loop", ids)
        self.assertIn("feed_bread", ids)
        self.assertNotIn("fun_deep_think", ids)
        self.assertNotIn("fun_tidy_dress", ids)
        self.assertNotIn("walk_slow_left", ids)
        self.assertNotIn("walk_left_v9_12", ids)
        self.assertNotIn("run_left_v9_12", ids)
        self.assertFalse(any("jump" in action_id for action_id in ids))

    def test_every_action_has_matching_frames_and_durations(self) -> None:
        for action in self.library.all():
            self.assertEqual(len(action.frames), len(action.duration_ms))
            self.assertTrue(all(path.is_file() for path in action.frames))


class StateMachineTests(unittest.TestCase):
    def test_drag_fall_land_idle_chain(self) -> None:
        machine = PetStateMachine()
        machine.dispatch(Event.PICK_UP)
        self.assertEqual(machine.state, PetState.DRAGGING)
        machine.dispatch(Event.RELEASE_AIRBORNE)
        self.assertEqual(machine.state, PetState.FALLING)
        machine.dispatch(Event.GROUND_COLLISION)
        self.assertEqual(machine.state, PetState.LANDING)
        machine.dispatch(Event.ACTION_FINISHED)
        self.assertEqual(machine.state, PetState.IDLE)


class FramePlayerTests(unittest.TestCase):
    def test_authored_durations_are_preserved(self) -> None:
        library = ActionLibrary(
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json"
        )
        action = library.get("land_recover_v4_12")
        player = FramePlayer()
        player.play(action)
        first = player.advance(0)
        self.assertIsNotNone(first)
        self.assertEqual(first.index, 0)
        last = player.advance(action.total_duration_ms)
        self.assertTrue(last.finished)
        self.assertEqual(last.index, len(action.frames) - 1)


class DragControllerTests(unittest.TestCase):
    def test_direction_and_tier_hysteresis(self) -> None:
        config = DragConfig(velocity_smoothing=1.0)
        drag = DragHybridController(config)
        drag.reset(0, 0, 0.0)
        fast = drag.update(120, 0, 0.05)
        self.assertEqual(fast.direction, "right")
        self.assertEqual(fast.speed_band, "fast")
        self.assertLess(fast.body_drag_angle, 0)
        self.assertLess(fast.root_drag_x, 0)
        # 700 px/s is below the fast exit (800) -> drops to medium.
        medium = drag.update(155, 0, 0.10)
        self.assertEqual(medium.speed_band, "medium")
        self.assertEqual(medium.pose_set, "drag_right_t02")
        # Still above the medium exit (200) -> stays medium.
        still = drag.update(175, 0, 0.15)
        self.assertEqual(still.speed_band, "medium")

    def test_tier_enter_thresholds_260_and_950(self) -> None:
        config = DragConfig(velocity_smoothing=1.0)
        drag = DragHybridController(config)
        drag.reset(0, 0, 0.0)
        slow = drag.update(10, 0, 0.05)  # 200 px/s -> slow
        self.assertEqual(slow.speed_band, "slow")
        medium = drag.update(26, 0, 0.10)  # (26-10)/0.05 = 320 -> medium
        self.assertEqual(medium.speed_band, "medium")
        fast = drag.update(100, 0, 0.15)  # (100-26)/0.05 = 1480 -> fast
        self.assertEqual(fast.speed_band, "fast")

    def test_vertical_does_not_fake_a_rotated_pose(self) -> None:
        drag = DragHybridController(DragConfig(velocity_smoothing=1.0))
        drag.reset(0, 0, 0.0)
        output = drag.update(0, -70, 0.05)
        self.assertEqual(output.direction, "up")
        self.assertIn("nearest_horizontal", output.pose_set)

    def test_dense_pose_never_skips_an_intermediate_point(self) -> None:
        drag = DragHybridController(DragConfig(velocity_smoothing=1.0))
        drag.reset(0, 0, 0.0)
        indices = [drag.update((i + 1) * 100, 0, (i + 1) * 0.05).pose_index for i in range(6)]
        self.assertEqual(indices, [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
