"""Pure geometry for multi-monitor desktop collision.

No Qt import here: this module is tested with the plain unittest runner.
The Qt adapter (qt_app) builds WorkArea objects from QGuiApplication screens,
so the physics below never assumes the primary screen starts at (0, 0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkArea:
    """A monitor's available desktop rectangle in virtual screen coordinates."""

    x: float
    y: float
    width: float
    height: float

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom


def ground_for_point(areas: tuple[WorkArea, ...], px: float, py: float) -> WorkArea | None:
    """Pick the work area that owns the point, or the nearest one.

    Prefers the area that actually contains the root point; when the point is
    outside every area (for example between monitors), falls back to the area
    with the smallest distance to the point so the character still has a
    ground to land on.
    """
    if not areas:
        return None
    for area in areas:
        if area.contains(px, py):
            return area
    best: WorkArea | None = None
    best_distance = math.inf
    for area in areas:
        dx = max(area.x - px, 0.0, px - area.right)
        dy = max(area.y - py, 0.0, py - area.bottom)
        distance = dx * dx + dy * dy
        if distance < best_distance:
            best_distance = distance
            best = area
    return best


def union_bounds(areas: tuple[WorkArea, ...]) -> WorkArea | None:
    """Bounding rectangle of every work area on the virtual desktop.

    Used as the drag cage: the character window stays inside this rectangle
    so it can be moved across monitors but can never be thrown off screen.
    """
    if not areas:
        return None
    min_x = min(area.x for area in areas)
    min_y = min(area.y for area in areas)
    max_right = max(area.right for area in areas)
    max_bottom = max(area.bottom for area in areas)
    return WorkArea(min_x, min_y, max_right - min_x, max_bottom - min_y)


def clamp_to_bounds(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class FallStep:
    """Result of integrating one falling tick."""

    root_x: float
    root_y: float
    vx: float
    vy: float
    hit: bool


def step_fall(
    root_x: float,
    root_y: float,
    vx: float,
    vy: float,
    dt_s: float,
    gravity: float,
    ground_y: float | None = None,
    air_drag: float = 0.0,
    max_speed: float | None = None,
) -> FallStep:
    """Advance world position with gravity using semi-implicit Euler.

    Collision is tested against the character's unified foot root node
    (``ground_y``), never against the transparent PNG bounding box: a frame
    whose pixels extend below the feet simply never counts as contact.
    """
    dt_s = max(0.0, dt_s)
    vy = vy + gravity * dt_s
    if max_speed is not None:
        vy = min(vy, max_speed)
    vx = vx * max(0.0, 1.0 - air_drag * dt_s)
    root_x = root_x + vx * dt_s
    root_y = root_y + vy * dt_s
    hit = ground_y is not None and root_y >= ground_y
    if hit:
        root_y = float(ground_y)
        vx = 0.0
        vy = 0.0
    return FallStep(root_x, root_y, vx, vy, hit)
