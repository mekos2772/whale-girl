"""Welcome-back greeting, post-interaction smile and flat-rig expressions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.action_library import ActionLibrary  # noqa: E402
from mimi_pet.config import load_config  # noqa: E402
from mimi_pet.engine import PetEngine  # noqa: E402
from mimi_pet.rig_model import load_rig_model  # noqa: E402
from mimi_pet.state_machine import Event, PetState  # noqa: E402


def _engine() -> PetEngine:
    config = load_config()
    return PetEngine(ActionLibrary(Path(config["asset_manifest"])), config)


def _back_to_idle(engine: PetEngine) -> None:
    engine.player.stop()
    engine.states.dispatch(Event.ACTION_FINISHED)


class WelcomeBackTests(unittest.TestCase):
    def test_wave_after_long_absence_only(self) -> None:
        engine = _engine()
        engine.tick(10.0, 0.0)
        self.assertFalse(engine.trigger_welcome_back(5.0))  # 离开太短
        self.assertTrue(engine.trigger_welcome_back(45.0))  # 长时间离开 -> 挥手
        self.assertEqual(engine.player.action.id, "wave")
        engine.tick(10.1, 0.0)  # bubble text lands on the next tick
        self.assertEqual(engine.bubble_text, "你回来啦～")

    def test_cooldown_blocks_repeat_greeting(self) -> None:
        engine = _engine()
        engine.tick(10.0, 0.0)
        self.assertTrue(engine.trigger_welcome_back(45.0))
        _back_to_idle(engine)
        self.assertFalse(engine.trigger_welcome_back(45.0))  # 冷却期内
        engine.now_s += engine.welcome_cooldown_s + 1.0
        self.assertTrue(engine.trigger_welcome_back(45.0))

    def test_no_wave_while_performing_or_dragging(self) -> None:
        engine = _engine()
        engine.tick(10.0, 0.0)
        engine.perform("sit_down")
        self.assertFalse(engine.trigger_welcome_back(60.0))
        _back_to_idle(engine)
        engine.begin_drag(0.0, 0.0, engine.now_s)
        self.assertFalse(engine.trigger_welcome_back(60.0))


class SmileAfterInteractionTests(unittest.TestCase):
    def test_touch_and_feed_leave_the_pet_smiling(self) -> None:
        engine = _engine()
        engine.tick(10.0, 0.0)
        engine.trigger_touch("head")
        self.assertGreater(engine._happy_until_s, 0.0)
        engine._happy_until_s = 0.0
        _back_to_idle(engine)
        self.assertEqual(engine.feed_bread(), "feed_bread")
        self.assertGreater(engine._happy_until_s, 0.0)

    def test_double_click_high_five_smiles(self) -> None:
        engine = _engine()
        engine.tick(1.0, 0.0)
        engine._happy_until_s = 0.0
        engine.trigger_double_click()
        self.assertEqual(engine.player.action.id, "high_five")
        self.assertGreater(engine._happy_until_s, 0.0)


class FlatRigExpressionTests(unittest.TestCase):
    """Flat (v5) rigs swap the whole master image for blink/smile variants."""

    def _flat_model(self, tmp: Path) -> Path:
        from PIL import Image

        source = tmp / "source"
        source.mkdir(parents=True)
        for name, colour in (
            ("master.png", (10, 20, 30, 255)),
            ("blink.png", (200, 100, 100, 255)),
            ("smile.png", (100, 200, 100, 255)),
        ):
            Image.new("RGBA", (64, 96), colour).save(source / name)
        model = {
            "name": "flat",
            "canvas": [64, 96],
            "layers": [
                {"name": "character_master", "file": "source/master.png", "pivot": [32, 90], "z": 1}
            ],
            "expressions": {
                "neutral": {"character_master": "source/master.png"},
                "blink": {"character_master": "source/blink.png"},
                "happy": {"character_master": "source/smile.png"},
            },
        }
        path = tmp / "model.json"
        path.write_text(json.dumps(model), encoding="utf-8")
        return path

    def test_loader_parses_flat_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            rig = load_rig_model(self._flat_model(Path(tmp_name)))
            self.assertEqual(rig.expressions["blink"], "source/blink.png")
            self.assertEqual(rig.expressions["happy"], "source/smile.png")
            self.assertEqual(rig.expressions["neutral"], "source/master.png")

    def test_loader_rejects_missing_expression_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            model_path = self._flat_model(tmp)
            (tmp / "source" / "blink.png").unlink()
            with self.assertRaises(FileNotFoundError):
                load_rig_model(model_path)

    def test_renderer_swaps_master_per_expression(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication

            from mimi_pet.image_cache import ImageCache
            from mimi_pet.renderer import QtRenderer
        except ImportError:
            self.skipTest("PySide6 not installed")

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp_name:
            rig = load_rig_model(self._flat_model(Path(tmp_name)))
            renderer = QtRenderer(ImageCache(), rig, (64, 96), {})
            self.assertTrue(renderer._flat_rig)
            self.assertEqual(renderer._master_expression_file("blink").name, "blink.png")
            self.assertEqual(renderer._master_expression_file("happy").name, "smile.png")
            # Unknown expressions fall back to the neutral master.
            self.assertEqual(renderer._master_expression_file("talk").name, "master.png")

    def test_speaking_flaps_between_open_and_closed_mouth(self) -> None:
        engine = _engine()
        engine.set_talking(True)
        engine._update_expression(1.00, engine.states.state)
        first = engine.expression
        engine._update_expression(1.10, engine.states.state)
        second = engine.expression
        self.assertEqual({first, second}, {"neutral", "talk"})


class FlatRigGazeTests(unittest.TestCase):
    """v5 whole-body attention must be visible: cursor extremes move the head
    band while the foot band stays planted (rotation is foot-anchored)."""

    def test_cursor_extremes_shift_the_head_band(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication

            from mimi_pet.image_cache import ImageCache
            from mimi_pet.renderer import QtRenderer, build_snapshot
        except ImportError:
            self.skipTest("PySide6 not installed")

        QApplication.instance() or QApplication([])
        config = load_config()
        engine = _engine()
        rig = load_rig_model(
            Path(config["asset_manifest"]).parent
            / json.load(open(config["asset_manifest"], encoding="utf-8"))["live_rig"]["model"]
        )
        renderer = QtRenderer(ImageCache(), rig, (engine.display_w, engine.display_h), config.get("rig"))

        def render_at(cursor_x: float):
            # Tick repeatedly so the smoothed gaze fully reaches the extreme.
            now = 100.0
            for _ in range(200):
                frame = engine.tick(now, 1.0 / 60.0, (cursor_x, engine.root_y - 100.0), 800.0)
                now += 1.0 / 60.0
            snapshot = build_snapshot(frame, engine, (engine.display_w, engine.display_h))
            return renderer.render(snapshot).toImage()

        left = render_at(engine.root_x - 600.0)
        right = render_at(engine.root_x + 600.0)

        # The v5 master's alpha bbox spans display rows ~154-373 (verified
        # against master_action_style_v1.png); head band vs foot band.
        def band_diff(a, b, y0, y1):
            count = 0
            for y in range(y0, y1):
                for x in range(0, engine.display_w):
                    ca, cb = a.pixelColor(x, y).rgb(), b.pixelColor(x, y).rgb()
                    if ca != cb:
                        count += 1
            return count

        head_diff = band_diff(left, right, 150, 245)
        foot_diff = band_diff(left, right, 345, engine.display_h)
        self.assertGreater(head_diff, 800, "头部区应随光标明显移动")
        self.assertLess(foot_diff, head_diff / 4, "脚部区应基本钉在地面")


if __name__ == "__main__":
    unittest.main()
