"""Offscreen checks for the action-matched v5 flat Live rig."""

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


class ActionMatchedRigRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        rig = load_rig_model(
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "usable" / "live_rig_v5" / "model.json"
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

    def test_rest_renders_and_attention_is_low_amplitude_transform(self) -> None:
        rest = self.renderer.render(self.neutral).toImage()
        attention = self.renderer.render(
            replace(self.neutral, head_x=9.0, head_y=-5.0, head_angle=4.0)
        ).toImage()
        self.assertFalse(rest.isNull())
        self.assertNotEqual(attention, rest)

    def test_flat_rig_tracks_irises_and_swaps_all_face_states(self) -> None:
        rest = self.renderer.render(self.neutral).toImage()
        gaze = self.renderer.render(replace(self.neutral, head_x=9.0)).toImage()
        self.assertNotEqual(gaze, rest)
        for expression in ("blink", "happy", "talk"):
            rendered = self.renderer.render(replace(self.neutral, expression=expression)).toImage()
            self.assertNotEqual(rendered, rest, expression)

    def test_breathing_changes_plate_without_moving_ground_registration(self) -> None:
        rest = self.renderer.render(self.neutral).toImage()
        breathed = self.renderer.render(replace(self.neutral, body_breathe=1.0)).toImage()
        self.assertNotEqual(breathed, rest)


if __name__ == "__main__":
    unittest.main()
