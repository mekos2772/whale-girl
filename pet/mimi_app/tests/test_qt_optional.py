"""Qt availability handling and Qt-gated tests.

The domain suite must run on machines without PySide6; Qt-dependent tests
are skipped there. ``test_domain_modules_import_without_qt`` proves it by
launching a subprocess with ``-S`` (site-packages disabled), where even the
venv's PySide6 is unavailable.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

try:
    from PySide6.QtCore import QSize  # noqa: F401
    from PySide6.QtGui import QPixmap

    HAS_QT = True
except ImportError:
    HAS_QT = False

if HAS_QT:
    from mimi_pet.image_cache import ImageCache  # noqa: E402
else:
    ImageCache = None  # type: ignore[assignment,misc]


class DomainWithoutQtTests(unittest.TestCase):
    def test_domain_modules_import_without_pyside6(self) -> None:
        # -S disables site-packages so PySide6 cannot be found even when the
        # current interpreter has it installed.
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import mimi_pet; "
            "import mimi_pet.collision, mimi_pet.rig_model, mimi_pet.engine, "
            "mimi_pet.scheduler, mimi_pet.frame_player, mimi_pet.live_controller, "
            "mimi_pet.drag_controller, mimi_pet.state_machine, mimi_pet.config; "
            "print('domain-ok')"
        ) % (ROOT / "mimi_app" / "src")
        result = subprocess.run(
            [sys.executable, "-S", "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("domain-ok", result.stdout)

    def test_domain_modules_never_import_pyside6(self) -> None:
        import re

        pattern = re.compile(r"^\s*(?:from PySide6|import PySide6)", re.MULTILINE)
        for module_name in (
            "action_library",
            "frame_player",
            "state_machine",
            "live_controller",
            "drag_controller",
            "scheduler",
            "engine",
            "config",
            "collision",
            "rig_model",
        ):
            source = (ROOT / "mimi_app" / "src" / "mimi_pet" / f"{module_name}.py").read_text(
                encoding="utf-8"
            )
            self.assertIsNone(pattern.search(source), f"{module_name}.py must stay Qt-free")


@unittest.skipUnless(HAS_QT, "PySide6 is not installed; Qt tests skipped")
class ImageCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtGui import QGuiApplication

        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_same_path_is_decoded_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pixel.png"
            # 2x2 opaque pixel, tiny file.
            from PySide6.QtGui import QColor, QImage

            image = QImage(2, 2, QImage.Format.Format_ARGB32)
            image.fill(QColor(120, 200, 90))
            self.assertTrue(image.save(str(path), "PNG"))

            decode_count = 0
            cache = ImageCache()

            def counting_loader() -> QPixmap:
                nonlocal decode_count
                decode_count += 1
                return QPixmap(str(path))

            first = cache.get_or_load("pixel.png", counting_loader)
            second = cache.get_or_load("pixel.png", counting_loader)
            self.assertIs(first, second)
            self.assertEqual(decode_count, 1)

    def test_scaled_pixmap_caches_decoded_and_scaled_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pixel.png"
            from PySide6.QtGui import QColor, QImage

            image = QImage(4, 4, QImage.Format.Format_ARGB32)
            image.fill(QColor(10, 20, 30))
            self.assertTrue(image.save(str(path), "PNG"))

            decode_count = 0
            cache = ImageCache()

            def counting_decode(inner_path: Path):
                nonlocal decode_count
                decode_count += 1
                return QPixmap(str(inner_path))

            cache._decode = counting_decode  # type: ignore[method-assign]
            size = QSize(2, 2)
            a = cache.scaled_pixmap(path, size)
            b = cache.scaled_pixmap(path, size)
            self.assertEqual(a.size(), size)
            self.assertIs(a, b)
            self.assertEqual(decode_count, 1)


if __name__ == "__main__":
    unittest.main()
