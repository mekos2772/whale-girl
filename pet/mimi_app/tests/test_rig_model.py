"""Rig model parsing tests (pure Python)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.rig_model import load_rig_model  # noqa: E402


class RigModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = load_rig_model(
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "usable" / "live_rig_v5" / "model.json"
        )

    def test_rig_loads_all_layers_in_z_order(self) -> None:
        self.assertEqual(len(self.model.layers), 1)
        names = [layer.name for layer in self.model.layers]
        self.assertEqual(
            names,
            ["character_master"],
        )
        z_values = [layer.z for layer in self.model.layers]
        self.assertEqual(z_values, sorted(z_values))
        for layer in self.model.layers:
            self.assertTrue(layer.file.is_file(), f"missing layer file: {layer.file}")

    def test_rig_uses_authored_pivots_and_calibration(self) -> None:
        master = self.model.layer("character_master")
        self.assertEqual(master.pivot, (512.0, 1488.0))
        self.assertAlmostEqual(master.base_scale, 1.0)
        self.assertEqual(master.base_offset, (0.0, 0.0))

    def test_rig_expressions_are_whole_face_switches(self) -> None:
        self.assertEqual(set(self.model.expressions), {"neutral", "blink", "happy", "talk"})
        self.assertEqual(self.model.expressions["blink"], "source/master_blink_v1.png")

    def test_rig_expression_patches_split_mouth_and_lids(self) -> None:
        """Region patches exist so blink can compose with smile/talk."""
        patches = self.model.expression_patches
        self.assertIsNotNone(patches)
        assert patches is not None
        self.assertEqual(patches.lids_blink.name, "lids_blink_v1.png")
        self.assertEqual(patches.mouth_happy.name, "mouth_happy_v1.png")
        self.assertEqual(patches.mouth_talk.name, "mouth_talk_v1.png")
        for path in (patches.lids_blink, patches.mouth_happy, patches.mouth_talk):
            self.assertTrue(path.is_file(), f"missing patch file: {path}")

    def test_flat_v5_uses_action_local_eye_layers(self) -> None:
        tracking = self.model.eye_tracking
        self.assertIsNotNone(tracking)
        assert tracking is not None
        self.assertEqual(tracking.max_offset, (16.0, 8.0))
        self.assertEqual(tracking.supported_expressions, ("neutral",))
        for path in (tracking.base_file, tracking.irises_file, tracking.clip_file, tracking.foreground_file):
            self.assertTrue(path.is_file())

    def test_missing_rig_file_raises(self) -> None:
        broken = ROOT / "mimi_app" / "tests" / "broken_rig_model.json"
        broken.write_text(
            '{"canvas": [1024, 1536], "layers": [{"name": "x", "file": "nope.png", "pivot": [1, 2], "z": 1}]}',
            encoding="utf-8",
        )
        try:
            with self.assertRaises(FileNotFoundError):
                load_rig_model(broken)
        finally:
            broken.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
