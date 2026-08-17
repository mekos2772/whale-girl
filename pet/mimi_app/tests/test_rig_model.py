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
            ROOT / "assets" / "characters" / "mimi" / "library_v1" / "usable" / "live_rig" / "model.json"
        )

    def test_rig_loads_all_layers_in_z_order(self) -> None:
        self.assertEqual(len(self.model.layers), 6)
        names = [layer.name for layer in self.model.layers]
        self.assertEqual(
            names,
            ["back_hair", "body_base", "arm_left", "arm_right", "head_expression", "skirt_hem"],
        )
        z_values = [layer.z for layer in self.model.layers]
        self.assertEqual(z_values, sorted(z_values))
        for layer in self.model.layers:
            self.assertTrue(layer.file.is_file(), f"missing layer file: {layer.file}")

    def test_rig_uses_authored_pivots_and_calibration(self) -> None:
        body = self.model.layer("body_base")
        self.assertEqual(body.pivot, (512.0, 1232.0))
        self.assertAlmostEqual(body.base_scale, 0.62)
        self.assertEqual(body.base_offset, (0.0, 52.0))
        self.assertEqual(self.model.layer("head_expression").pivot, (512.0, 654.0))
        self.assertTrue(self.model.layer("skirt_hem").experimental)

    def test_rig_expressions_are_whole_face_switches(self) -> None:
        self.assertEqual(
            set(self.model.expressions),
            {"neutral", "blink", "talk", "happy"},
        )
        head = self.model.layer("head_expression")
        for filename in self.model.expressions.values():
            self.assertIn(filename, head.switches)
            self.assertTrue((head.file.parent / filename).is_file())

    def test_neutral_eye_tracking_uses_original_iris_layers(self) -> None:
        tracking = self.model.eye_tracking
        self.assertIsNotNone(tracking)
        assert tracking is not None
        self.assertEqual(tracking.supported_expressions, ("neutral",))
        self.assertEqual(tracking.max_offset, (8.0, 4.0))
        self.assertEqual(tracking.source_parameters, ("head_x", "head_y"))
        for path in (
            tracking.base_file,
            tracking.irises_file,
            tracking.clip_file,
            tracking.foreground_file,
        ):
            self.assertTrue(path.is_file(), f"missing eye layer: {path}")

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
