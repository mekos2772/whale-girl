"""Body-part touch regions, combos and the idle rest ladder (sit → sleep)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary
from mimi_pet.config import load_config
from mimi_pet.engine import PetEngine, touch_region
from mimi_pet.state_machine import Event, PetState


def make_engine() -> PetEngine:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    return PetEngine(library, config)


class TouchRegionMappingTests(unittest.TestCase):
    def test_head_face_belly_bands(self) -> None:
        self.assertEqual(touch_region(0.50, 0.45), "head")
        self.assertEqual(touch_region(0.50, 0.55), "face")
        self.assertEqual(touch_region(0.50, 0.70), "belly")

    def test_hair_sides_count_as_head(self) -> None:
        self.assertEqual(touch_region(0.15, 0.58), "head")
        self.assertEqual(touch_region(0.90, 0.58), "head")

    def test_hands_and_feet_zones(self) -> None:
        self.assertEqual(touch_region(0.15, 0.75), "hand")
        self.assertEqual(touch_region(0.85, 0.70), "hand")
        self.assertEqual(touch_region(0.50, 0.93), "foot")


class RegionReactionTests(unittest.TestCase):
    def test_hand_click_high_fives(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        self.assertTrue(engine.trigger_touch("hand"))
        self.assertEqual(engine.player.action.id, "high_five")

    def test_foot_click_dodges_sideways(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        self.assertTrue(engine.trigger_touch("foot", x_ratio=0.3))
        self.assertEqual(engine.player.action.id, "walk_right")
        engine2 = make_engine()
        engine2.place_at(500.0, 800.0)
        self.assertTrue(engine2.trigger_touch("foot", x_ratio=0.7))
        self.assertEqual(engine2.player.action.id, "walk_left")

    def test_touch_reactions_update_affection_only_after_success(self) -> None:
        engine = make_engine()
        before = engine.affection_score
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.affection_score, before + 1)
        engine.player.stop()
        engine.states.dispatch(Event.ACTION_FINISHED)
        # Repeating too soon is rate-limited rather than farming points.
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.affection_score, before + 1)

    def test_head_hand_combo_has_no_duplicate_head_reward(self) -> None:
        engine = make_engine()
        engine.now_s = 10.0
        self.assertTrue(engine.trigger_touch("head"))
        engine.player.stop()
        engine.states.dispatch(Event.ACTION_FINISHED)
        engine.now_s = 11.5
        self.assertTrue(engine.trigger_touch("hand"))
        self.assertEqual(engine.affection_score, 53)

    def test_rejected_foot_touch_does_not_update_affection(self) -> None:
        engine = make_engine()
        before = engine.affection_score
        engine.perform("sit_down")
        self.assertFalse(engine.trigger_touch("foot"))
        self.assertEqual(engine.affection_score, before)

    def test_head_then_hand_celebrates(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.now_s = 10.0
        self.assertTrue(engine.trigger_touch("head"))
        engine.now_s = 11.5  # still inside the 3 s combo window
        self.assertTrue(engine.trigger_touch("hand"))
        self.assertEqual(engine.player.action.id, "celebrate")

    def test_combo_expires_after_window(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.now_s = 10.0
        engine.trigger_touch("head")
        engine.now_s = 15.0  # far past the combo window
        engine.player.stop()
        engine.states.dispatch(Event.ACTION_FINISHED)
        self.assertTrue(engine.trigger_touch("hand"))
        self.assertEqual(engine.player.action.id, "high_five")

    def test_triple_touch_escalates_with_bubble(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        for t in (10.0, 10.4, 10.8):
            engine.now_s = t
            engine.trigger_touch("belly")
        # The third rapid touch raised a delighted bubble text.
        self.assertIn("别挠", engine._external_bubble or "")


class IdleRestLadderTests(unittest.TestCase):
    def test_long_idle_sits_then_sleeps(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        self.assertTrue(engine.scenario_check(70.0, random_value=0.9))
        self.assertEqual(engine.player.action.id, "sit_down")
        # A sit long enough escalates to sleep.
        self.assertTrue(engine.scenario_check(320.0, random_value=0.9))
        self.assertEqual(engine.states.state, PetState.SLEEPING)
        self.assertEqual(engine.player.action.id, "sleep_lie_down")

    def test_touch_while_sleeping_wakes(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        engine.scenario_check(320.0, random_value=0.9)
        engine.now_s = 321.0
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.player.action.id, "wake_up")

    def test_interrupt_rest_stands_a_sitting_pet(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        engine.scenario_check(70.0, random_value=0.9)
        self.assertTrue(engine.is_sitting)
        engine.interrupt_rest()
        self.assertEqual(engine.player.action.id, "stand_up")

    def test_touch_while_sitting_still_reacts(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        engine.scenario_check(70.0, random_value=0.9)
        engine.now_s = 71.0
        self.assertTrue(engine.trigger_touch("head"))
        self.assertEqual(engine.player.action.id, "head_pat")

    def test_feed_pops_out_of_sit(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        engine.scenario_check(70.0, random_value=0.9)
        engine.now_s = 71.0
        self.assertEqual(engine.feed_bread(), "feed_bread")

    def test_double_click_while_sitting_stands_up(self) -> None:
        engine = make_engine()
        engine.place_at(500.0, 800.0)
        engine.note_input(0.0)
        engine.scenario_check(70.0, random_value=0.9)
        engine.now_s = 71.0
        self.assertTrue(engine.trigger_double_click())
        self.assertEqual(engine.player.action.id, "stand_up")


if __name__ == "__main__":
    unittest.main()
