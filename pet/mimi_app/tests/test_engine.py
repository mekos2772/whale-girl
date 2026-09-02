"""Engine integration tests: drag cancellation, fall/land chain, idle return."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.frame_player import FramePlayer  # noqa: E402
from mimi_pet.state_machine import Event, PetState, PetStateMachine  # noqa: E402


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


class EngineFlowTests(unittest.TestCase):
    def test_dragging_cancels_running_performance(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        self.assertTrue(engine.perform("fun_facepalm"))
        self.assertEqual(engine.states.state, PetState.PERFORMING)
        self.assertIsNotNone(engine.player.action)
        engine.begin_drag(600.0, 700.0, 0.0)
        self.assertEqual(engine.states.state, PetState.DRAGGING)
        self.assertIsNone(engine.player.action, "pick-up must cancel the running performance")

    def test_falling_collision_enters_landing_and_loads_land_recover(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 300.0)  # feet well above the ground
        engine.begin_drag(500.0, 300.0, 0.0)
        self.assertEqual(engine.release(ground_y=800.0, timestamp_s=1.0), "falling")
        self.assertEqual(engine.states.state, PetState.FALLING)
        engine.collide_ground(800.0)
        self.assertEqual(engine.states.state, PetState.LANDING)
        self.assertIsNotNone(engine.player.action)
        self.assertEqual(engine.player.action.id, "land_recover_v4_12")
        self.assertEqual(engine.root_y, 800.0)

    def test_falling_physics_reaches_ground_and_triggers_collision(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 100.0)
        engine.begin_drag(500.0, 100.0, 0.0)
        engine.release(ground_y=800.0, timestamp_s=1.0)
        now = 1.0
        steps = 0
        while engine.states.state is PetState.FALLING and steps < 300:
            now += 1.0 / 60.0
            engine.tick(now, 1.0 / 60.0, (500.0, 100.0), 800.0)
            steps += 1
        self.assertEqual(engine.states.state, PetState.LANDING)
        self.assertAlmostEqual(engine.root_y, 800.0, delta=0.5)
        self.assertEqual(engine.player.action.id, "land_recover_v4_12")

    def test_landing_playback_finishes_back_to_idle(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 300.0)
        engine.begin_drag(500.0, 300.0, 0.0)
        engine.release(ground_y=800.0, timestamp_s=1.0)
        engine.collide_ground(800.0)
        self.assertEqual(engine.states.state, PetState.LANDING)
        # The engine clamps per-tick dt (0.1 s max), so advance frame by frame
        # until the landing action finishes.
        now = 1.0
        for _ in range(200):
            now += 1.0 / 60.0
            engine.tick(now, 1.0 / 60.0, None, 800.0)
            if engine.states.state is PetState.IDLE:
                break
        self.assertEqual(engine.states.state, PetState.IDLE)
        self.assertIsNone(engine.player.action)

    def test_release_grounded_goes_straight_to_landing(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 799.0)  # feet essentially on the ground
        engine.begin_drag(500.0, 799.0, 0.0)
        result = engine.release(ground_y=800.0, timestamp_s=1.0)
        self.assertEqual(result, "landing")
        self.assertEqual(engine.states.state, PetState.LANDING)
        self.assertEqual(engine.player.action.id, "land_recover_v4_12")

    def test_drag_keeps_pickup_anchor_offset(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(560.0, 760.0, 0.0)
        self.assertEqual(engine.grab_dx, 60.0)
        self.assertEqual(engine.grab_dy, -40.0)
        output = engine.update_drag(900.0, 600.0, 0.5)
        self.assertIsNotNone(output)
        # Root follows the cursor minus the original anchor offset.
        self.assertAlmostEqual(engine.root_x, 840.0)
        self.assertAlmostEqual(engine.root_y, 640.0)

    def test_force_perform_replaces_running_action(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.perform("fun_facepalm")
        self.assertTrue(engine.force_perform("wave"))
        self.assertEqual(engine.player.action.id, "wave")
        self.assertEqual(engine.states.state, PetState.PERFORMING)
        # Cannot force-perform while dragging.
        engine.begin_drag(600.0, 700.0, 0.0)
        self.assertFalse(engine.force_perform("fun_facepalm"))
        self.assertEqual(engine.states.state, PetState.DRAGGING)

    def test_expression_priority_blocks_blink_on_locked_actions(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine._next_blink_s = 0.0  # force an immediate blink
        engine.perform("fun_facepalm")  # explicit-face action
        engine.tick(0.5, 0.016, (500.0, 800.0))
        self.assertEqual(engine.expression, "neutral")
        # A non-locked action may blink.
        engine.force_perform("wave")
        engine._next_blink_s = 0.0
        engine.tick(1.0, 0.016, (500.0, 800.0))
        self.assertEqual(engine.expression, "blink")

    def test_mouth_and_blink_are_independent_channels(self) -> None:
        """A random blink must never erase an active smile (the legacy fight)."""
        engine = make_engine()
        engine.set_happy(10.0)
        engine._next_blink_s = 0.0
        engine._update_expression(5.0, PetState.IDLE)
        self.assertEqual(engine._mouth, "happy")
        self.assertTrue(engine._eyes_closed)
        # After the 0.14 s blink the smile is still there.
        engine._update_expression(5.3, PetState.IDLE)
        self.assertEqual(engine._mouth, "happy")
        self.assertFalse(engine._eyes_closed)
        # Talking occupies the mouth channel while the eyes stay open.
        engine.set_talking(True)
        engine._update_expression(5.4, PetState.IDLE)
        self.assertIn(engine._mouth, ("talk", "neutral"))
        self.assertFalse(engine._eyes_closed)

    def test_drag_plays_registered_pose_loop_and_switches_sets(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        # First update: raw 400 px/s -> smoothed ~112 px/s -> slow tier.
        engine.update_drag(520.0, 800.0, 0.05)
        self.assertEqual(engine._current_pose_set, "drag_right_t00")
        frame = engine.tick(0.1, 0.05, (510.0, 800.0))
        self.assertIsNotNone(frame.drag_pose_path)
        self.assertEqual(frame.drag_pose_set, "drag_right_t00")
        self.assertIsNotNone(frame.drag_pose_index)
        path_before = frame.drag_pose_path
        # Same set keeps looping the same frames.
        engine.tick(0.15, 0.05, (510.0, 800.0))
        # Fast right motion switches to the fast set.
        for i in range(20):
            engine.update_drag(560.0 + i * 40.0, 800.0, 0.2 + i * 0.01)
        engine.tick(0.5, 0.05, (1200.0, 800.0))
        self.assertEqual(engine._current_pose_set, "drag_right_t10")
        # Releasing stops the pose loop.
        engine.release(ground_y=800.0, timestamp_s=0.6)
        engine.tick(0.7, 0.05, (1200.0, 800.0), 800.0)
        frame = engine.tick(0.75, 0.05, (1200.0, 800.0), 800.0)
        self.assertIsNone(frame.drag_pose_path)

    def test_vertical_drag_reuses_nearest_horizontal_pose_set(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        engine.update_drag(520.0, 800.0, 0.05)  # raw 400 px/s -> slow, right
        self.assertEqual(engine._current_pose_set, "drag_right_t00")
        # Strong vertical motion: no invented vertical set, keep nearest
        # horizontal sheet at the current tier (medium).
        engine.update_drag(510.0, 700.0, 0.1)  # raw vy = -2000 -> medium tier
        self.assertEqual(engine._current_pose_set, "drag_right_t01")
        frame = engine.tick(0.15, 0.05, (510.0, 700.0))
        self.assertIsNotNone(frame.drag_pose_path)

    def test_hold_keeps_standing_rig_not_crouch_pose(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        # A light click with almost no motion must not flash the picked-up
        # crouch pose: hold tier keeps the standing rig.
        engine.update_drag(501.0, 800.0, 0.05)  # 20 px/s -> hold
        self.assertEqual(engine.last_drag.speed_band, "hold")
        self.assertIsNone(engine.drag_pose_player.action)
        frame = engine.tick(0.1, 0.05, (501.0, 800.0))
        self.assertIsNone(frame.drag_pose_path)
        # Real drag motion switches to the slow pose.
        engine.update_drag(540.0, 800.0, 0.15)  # raw (540-501)/0.1 = 390 -> slow
        self.assertEqual(engine.last_drag.speed_band, "slow")
        self.assertIsNotNone(engine.drag_pose_player.action)
        frame = engine.tick(0.2, 0.05, (520.0, 800.0))
        self.assertIsNotNone(frame.drag_pose_path)

    def test_hold_settles_to_t00_without_a_rebound_animation(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        engine.update_drag(540.0, 800.0, 0.05)  # slow tier, right
        engine.update_drag(560.0, 800.0, 0.1)
        self.assertIsNotNone(engine.drag_pose_player.action)
        # Stop moving: smoothed velocity decays and the dense pose index walks
        # back to t00. No separate reverse-sheet rebound is allowed.
        engine.update_drag(560.0, 800.0, 0.15)
        self.assertEqual(engine.last_drag.speed_band, "slow")
        for _ in range(20):
            engine.update_drag(560.0, 800.0, 0.2 + _ * 0.05)
        self.assertEqual(engine.last_drag.speed_band, "hold")
        self.assertEqual(engine.last_drag.pose_index, 0)
        self.assertEqual(engine._current_pose_set, "drag_right_t00")
        frame = engine.tick(1.3, 1.0 / 60.0, (560.0, 800.0))
        self.assertIsNotNone(frame.drag_pose_path)
        self.assertEqual(frame.drag_pose_set, "drag_right_t00")

    def test_resume_motion_advances_from_t00(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        engine.update_drag(540.0, 800.0, 0.05)
        engine.update_drag(560.0, 800.0, 0.1)
        engine.update_drag(560.0, 800.0, 0.15)
        for _ in range(20):
            engine.update_drag(560.0, 800.0, 0.2 + _ * 0.05)
        self.assertEqual(engine.last_drag.pose_index, 0)
        engine.update_drag(600.0, 800.0, 1.3)  # motion resumes
        engine.update_drag(660.0, 800.0, 1.35)
        self.assertEqual(engine.last_drag.pose_index, 1)
        self.assertEqual(engine._current_pose_set, "drag_right_t01")

    def test_external_bubble_is_exposed_in_frame(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.set_external_bubble("测试气泡", 2.0)
        frame = engine.tick(0.1, 1.0 / 60.0, (500.0, 800.0))
        self.assertEqual(frame.bubble_text, "测试气泡")

    def test_landing_returns_directly_to_idle(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 300.0)
        engine.begin_drag(500.0, 300.0, 0.0)
        engine.release(ground_y=800.0, timestamp_s=1.0)
        engine.collide_ground(800.0)
        for _ in range(200):
            engine.tick(1.1 + _ * 1.0 / 60.0, 1.0 / 60.0, None, 800.0)
            if engine.states.state is PetState.IDLE and engine.player.action is None:
                break
        self.assertEqual(engine.states.state, PetState.IDLE)
        self.assertIsNone(engine.player.action)

    def test_scenario_stays_idle_before_walk_threshold(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        self.assertFalse(engine.scenario_check(1.0, random_value=0.0))
        self.assertEqual(engine.states.state, PetState.IDLE)

    def test_scenario_idle_triggers_core_walk(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        self.assertTrue(engine.scenario_check(20.0, random_value=0.0))
        self.assertIn(engine.player.action.id, {"walk_left", "walk_right"})

    def test_scenario_has_no_random_full_frame_ambience(self) -> None:
        from mimi_pet.engine import AMBIENCE_ACTIONS
        self.assertEqual(AMBIENCE_ACTIONS, frozenset())

    def test_random_performance_pool_is_pruned(self) -> None:
        library = make_engine().library
        ids = {action.id for action in library.random_performances()}
        self.assertNotIn("fun_tidy_dress", ids)
        self.assertNotIn("fun_deep_think", ids)
        self.assertEqual(ids, set())

    def test_free_placement_release_stays_put(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 300.0)
        engine.begin_drag(500.0, 300.0, 0.0)
        engine.set_free_placement(True)
        result = engine.release(ground_y=800.0, timestamp_s=1.0)
        self.assertEqual(result, "landing")
        self.assertEqual(engine.states.state, PetState.IDLE)
        self.assertIsNone(engine.player.action)
        self.assertEqual(engine.root_y, 300.0)  # no fall

    def test_click_react_pulses_then_clears(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.tick(0.0, 1.0 / 60.0, None, 800.0)
        engine.trigger_click_react()
        frame = engine.tick(0.05, 1.0 / 60.0, None, 800.0)
        self.assertGreater(frame.click_react, 0.0)
        # After 0.35 s the pulse is gone.
        for _ in range(30):
            frame = engine.tick(0.1 + _ * 1.0 / 60.0, 1.0 / 60.0, None, 800.0)
        self.assertEqual(frame.click_react, 0.0)

    def test_display_size_update_recomputes_pivot(self) -> None:
        engine = make_engine()
        engine.set_display_size(192, 288)
        self.assertEqual(engine.display_w, 192)
        self.assertAlmostEqual(engine.pivot_display_x, 96.0)
        self.assertAlmostEqual(engine.pivot_display_y, 279.0)
        engine.set_display_size(320, 480)
        self.assertAlmostEqual(engine.pivot_display_x, 160.0)
        self.assertAlmostEqual(engine.pivot_display_y, 465.0)

    def test_drag_cage_keeps_window_on_the_virtual_desktop(self) -> None:
        from mimi_pet.collision import WorkArea

        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.begin_drag(500.0, 800.0, 0.0)
        bounds = WorkArea(0.0, 0.0, 1920.0, 1040.0)
        # Try to drag the root far outside the bounds.
        engine.update_drag(100000.0, 100000.0, 0.05, bounds=bounds)
        self.assertLessEqual(engine.root_x, bounds.right - (engine.display_w - engine.pivot_display_x))
        self.assertGreaterEqual(engine.root_x, bounds.x + engine.pivot_display_x)
        self.assertLessEqual(engine.root_y, bounds.bottom - (engine.display_h - engine.pivot_display_y))
        self.assertGreaterEqual(engine.root_y, bounds.y + engine.pivot_display_y)

    def test_falling_horizontal_cage_stops_off_screen_flight(self) -> None:
        engine = make_engine()
        engine.place_at(-200.0, 300.0)  # already outside the left cage edge
        engine.begin_drag(-200.0, 300.0, 0.0)
        engine.release(ground_y=800.0, timestamp_s=1.0)
        engine.vx = -4000.0
        engine.tick(1.1, 1.0 / 60.0, None, 800.0, x_bounds=(-100.0, 1000.0))
        self.assertGreaterEqual(engine.root_x, -100.0)
        self.assertEqual(engine.vx, 0.0)


class InteractionTests(unittest.TestCase):
    def test_walk_moves_root_and_stops_at_target_without_resizing_frames(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        self.assertTrue(engine.start_walk("right", target_x=510.0))
        self.assertEqual(engine.player.action.id, "walk_right")
        for index in range(20):
            engine.tick(index * 0.05, 0.05, None, 800.0, (100.0, 900.0))
            if engine.states.state is PetState.IDLE:
                break
        self.assertEqual(engine.root_x, 510.0)
        self.assertEqual(engine.states.state, PetState.IDLE)

    def test_sit_transitions_to_loop_then_stands(self) -> None:
        engine = make_engine()
        self.assertTrue(engine.start_sit())
        for index in range(500):
            engine.tick(index / 60.0, 1.0 / 60.0)
            if engine.player.action is not None and engine.player.action.id == "sit_idle":
                break
        self.assertEqual(engine.states.state, PetState.PERFORMING)
        self.assertEqual(engine.player.action.id, "sit_idle")
        self.assertTrue(engine.stand_up())
        for index in range(500, 1000):
            engine.tick(index / 60.0, 1.0 / 60.0)
            if engine.states.state is PetState.IDLE:
                break
        self.assertEqual(engine.states.state, PetState.IDLE)

    def test_sleep_chain_loop_and_wake(self) -> None:
        engine = make_engine()
        self.assertTrue(engine.start_sleep())
        for index in range(700):
            engine.tick(index / 60.0, 1.0 / 60.0)
            if engine.player.action is not None and engine.player.action.id == "sleep_loop":
                break
        self.assertEqual(engine.states.state, PetState.SLEEPING)
        self.assertEqual(engine.player.action.id, "sleep_loop")
        engine.tick(engine.now_s + 1.0 / 60.0, 1.0 / 60.0)
        self.assertEqual(engine.player.action.id, "sleep_loop")
        self.assertTrue(engine.wake_up())
        for index in range(700):
            engine.tick(engine.now_s + 1.0 / 60.0, 1.0 / 60.0)
            if engine.states.state is PetState.IDLE:
                break
        self.assertEqual(engine.states.state, PetState.IDLE)

    def test_touch_regions_and_feed_satiety(self) -> None:
        engine = make_engine()
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.player.action.id, "head_pat")
        engine.player.stop()
        engine.states.dispatch(Event.ACTION_FINISHED)
        engine.satiety = 0.0
        self.assertEqual(engine.feed_bread(), "feed_bread")
        engine.player.stop()
        engine.states.dispatch(Event.ACTION_FINISHED)
        engine.satiety = 1.0
        self.assertEqual(engine.feed_bread(), "feed_refuse")

    def test_click_uses_live_squash_without_extra_asset(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.trigger_click_react(random_value=0.0)
        self.assertEqual(engine.states.state, PetState.IDLE)
        self.assertGreater(engine.tick(0.05, 1.0 / 60.0).click_react, 0.0)

    def test_double_click_plays_an_action(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.trigger_double_click()
        self.assertEqual(engine.states.state, PetState.PERFORMING)
        self.assertIsNotNone(engine.player.action)

    def test_hover_itself_triggers_nothing(self) -> None:
        # Hover attention is continuous Live gaze tracking; only a pointer
        # RETURNING after a long absence waves (see test_v5_interactions).
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.tick(10.0, 0.0)
        self.assertFalse(engine.trigger_welcome_back(2.0))  # 短暂离开不打招呼
        self.assertEqual(engine.states.state, PetState.IDLE)
        self.assertIsNone(engine.player.action)

    def test_fast_release_uses_compact_landing(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 300.0)
        engine.begin_drag(500.0, 300.0, 0.0)
        # fast fling: set vx high, then release (docked fall).
        engine.vx = 2500.0
        self.assertEqual(engine.release(ground_y=800.0, timestamp_s=1.0), "falling")
        self.assertTrue(engine._slung_release)
        # The slim profile reuses one authored landing for all impact speeds.
        reached = False
        for _ in range(1000):
            if engine.states.state is PetState.FALLING and engine.root_y >= 800.0 - 2:
                engine.collide_ground(800.0)
            engine.tick(1.0, 1.0 / 60.0, None, 800.0, None)
            if engine.player.action is not None and engine.player.action.id == "land_recover_v4_12":
                reached = True
                break
        self.assertTrue(reached, "compact landing never played")


if __name__ == "__main__":
    unittest.main()
