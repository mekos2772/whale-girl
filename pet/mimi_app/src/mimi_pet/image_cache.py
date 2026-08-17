"""QPixmap cache: every PNG on disk is decoded exactly once per path.

Qt-only module; imported exclusively by the GUI adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap


class ImageCache:
    def __init__(self) -> None:
        self._pixmaps: dict[str, QPixmap] = {}
        self._scaled: dict[tuple[str, int, int], QPixmap] = {}
        self._generic: dict[Any, Any] = {}

    # -- generic loader with an explicit decoder (used by tests to count
    #    disk decodes)
    def get_or_load(self, key: Any, loader: Callable[[], Any]) -> Any:
        if key not in self._generic:
            self._generic[key] = loader()
        return self._generic[key]

    # -- pixmap API
    def _decode(self, path: Path) -> QPixmap:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            raise FileNotFoundError(f"cannot decode image: {path}")
        return pixmap

    def pixmap(self, path: Path) -> QPixmap:
        key = str(path.resolve())
        if key not in self._pixmaps:
            self._pixmaps[key] = self._decode(path)
        return self._pixmaps[key]

    def scaled_pixmap(self, path: Path, size: QSize) -> QPixmap:
        """Decode once, scale once, then reuse the scaled result."""
        key = (str(path.resolve()), size.width(), size.height())
        cached = self._scaled.get(key)
        if cached is None:
            cached = self.pixmap(path).scaled(
                size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self._scaled[key] = cached
        return cached

    def clear(self) -> None:
        self._pixmaps.clear()
        self._scaled.clear()
        self._generic.clear()
