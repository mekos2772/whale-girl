"""Offscreen checks for the independent neutral-eye compositor."""

from __future__ import annotations

from dataclasses import replace
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from mimi_pet.image_cache import ImageCache  # noqa: E402
from mimi_pet.renderer import QtRenderer, RenderSnapshot  # noqa: E402
from mimi_pet.rig_model import load_rig_model  # noqa: E402


class EyeTrackingRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        rig = load_rig_model(
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "usable" / "live_rig" / "model.json"
        )
        cls.renderer = QtRenderer(ImageCache(), rig, (512, 768))
        cls.neutral = RenderSnapshot(
            mode="rig",
            frame_path=None,
            expression="neutral",
            state="IDLE",
            action_id=None,
            frame_index=None,
            frame_total=None,
            root_x=0,
            root_y=0,
            vx=0,
            vy=0,
            grab_dx=0,
            grab_dy=0,
            direction="",
            speed_band="",
            pose_set="",
            drag_speed=0,
            body_tilt=0,
            hair_lag=0,
            skirt_lag=0,
            display_size=(512, 768),
            pivot_display=(256, 744),
            ground_y=744,
        )

    def test_rest_uses_exact_reviewed_neutral_and_gaze_uses_composite(self) -> None:
        head_file = self.renderer._head_file("neutral")
        direct = self.renderer._layer_pixmap("head_expression", head_file).toImage()
        rest = self.renderer._head_pixmap(self.neutral, head_file).toImage()
        gaze = self.renderer._head_pixmap(
            replace(self.neutral, head_x=9.0, head_y=-5.0), head_file
        ).toImage()
        self.assertEqual(rest, direct)
        self.assertNotEqual(gaze, direct)

    def test_non_neutral_expression_bypasses_eye_compositor(self) -> None:
        blink = replace(self.neutral, expression="blink", head_x=9.0, head_y=7.0)
        blink_file = self.renderer._head_file("blink")
        result = self.renderer._head_pixmap(blink, blink_file).toImage()
        direct = self.renderer._layer_pixmap("head_expression", blink_file).toImage()
        self.assertEqual(result, direct)


if __name__ == "__main__":
    unittest.main()
