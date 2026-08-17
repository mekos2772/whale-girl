"""Allowlist strictness and per-frame duration tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.frame_player import FramePlayer  # noqa: E402


class AllowlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.library = ActionLibrary(ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json")

    def test_candidates_rejected_retired_actions_cannot_be_loaded(self) -> None:
        # Walk/run were explicitly rejected by the user.
        for bad in ("walk_left_v9_12", "run_left_v9_12"):
            with self.assertRaises(KeyError, msg=bad):
                self.library.get(bad)
        # All jump/hop variants are retired.
        for bad in ("hop", "jump_left", "jump_right", "hop_jump_left_12", "hop_jump_right_v1"):
            with self.assertRaises(KeyError, msg=bad):
                self.library.get(bad)
        # Candidate drag/vertical/click assets and old low-density actions
        # must never enter the runtime path either.
        for bad in (
            "drag_left_slow_v7_main6",
            "drag_right_fast_v6_main6",
            "drag_vertical",
            "click_head_v5_main6",
            "click_belly_v5_main6",
            "click_react_v5_main6",
            "drop_left_v6_main6",
            "wave",
            "greet",
            "kiss",
            "cry",
            "sleep",
            "sit",
            "stretch",
            "listen",
        ):
            with self.assertRaises(KeyError, msg=bad):
                self.library.get(bad)
        # The retired jump family never shows up in the whole library.
        ids = {action.id for action in self.library.all()}
        self.assertFalse(any("jump" in action_id or "hop" in action_id for action_id in ids))

    def test_harness_actions_registered_and_excluded_from_random(self) -> None:
        for action_id in ("harness_task_thinking_v1_12", "harness_tool_working_v1_12"):
            action = self.library.get(action_id)
            self.assertEqual(action.trigger, "dsh_activity", action_id)
            self.assertEqual(len(action.frames), 12, action_id)
            self.assertTrue(all(path.is_file() for path in action.frames), action_id)
            self.assertEqual(tuple(action.canvas), (512, 768), action_id)
            self.assertGreater(sum(action.duration_ms), 4000, action_id)  # slowed down


class DragPoseRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.library = ActionLibrary(ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json")

    def test_drag_pose_sets_are_registered_with_frames(self) -> None:
        sets = {pose.id for pose in self.library.drag_pose_sets()}
        self.assertEqual(sets, {f"drag_{direction}_t{index:02d}" for direction in ("left", "right") for index in range(11)})
        for set_id in sets:
            pose = self.library.drag_pose(set_id)
            self.assertTrue(pose.loop, set_id)
            self.assertEqual(len(pose.frames), 1, set_id)  # v13 direct force points
            self.assertTrue(all(path.is_file() for path in pose.frames), set_id)
            self.assertTrue(all(duration > 0 for duration in pose.duration_ms), set_id)

    def test_drag_poses_are_not_full_frame_actions(self) -> None:
        ids = {action.id for action in self.library.all()}
        self.assertFalse(ids & {"drag_left_t00", "drag_right_t10"})
        with self.assertRaises(KeyError):
            self.library.drag_pose("drag_vertical")  # never registered


class FramePlayerVariedDurationTests(unittest.TestCase):
    """FramePlayer must honor each frame's authored duration, not a uniform FPS."""

    def test_player_crosses_multiple_distinct_durations(self) -> None:
        library = ActionLibrary(ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json")
        # fun_facepalm has deliberately uneven durations.
        action = library.get("fun_facepalm")
        durations = action.duration_ms
        self.assertGreater(len(set(durations)), 3)
        player = FramePlayer()
        player.play(action)

        sample = player.advance(0)
        self.assertEqual(sample.index, 0)
        self.assertFalse(sample.finished)

        # Exactly at the boundary of the first four frames -> still frame 4.
        sample = player.advance(sum(durations[:4]))
        self.assertEqual(sample.index, 4)
        self.assertFalse(sample.finished)

        # One more authored duration later -> frame 5.
        sample = player.advance(durations[4])
        self.assertEqual(sample.index, 5)

        # Skip to the very end: finished, last frame held.
        sample = player.advance(action.total_duration_ms)
        self.assertTrue(sample.finished)
        self.assertEqual(sample.index, len(action.frames) - 1)

    def test_landing_durations_are_also_respected(self) -> None:
        library = ActionLibrary(ROOT / "assets" / "characters" / "mimi" / "library_v1" / "manifest.json")
        action = library.get("land_recover_v4_12")
        player = FramePlayer()
        player.play(action)
        # Frame 1 is 85 ms; at 80 ms we are still on frame 0.
        self.assertEqual(player.advance(80).index, 0)
        # At 90 ms we have crossed into frame 1 (75 ms long).
        self.assertEqual(player.advance(10).index, 1)


if __name__ == "__main__":
    unittest.main()
