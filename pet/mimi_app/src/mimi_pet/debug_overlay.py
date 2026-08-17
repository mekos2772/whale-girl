"""Toggleable debug overlay drawn on top of the pet window.

Qt-only module; the overlay only consumes a RenderSnapshot and never touches
the state machine.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter

from .renderer import RenderSnapshot


def debug_lines(snapshot: RenderSnapshot) -> list[str]:
    frame = f"{snapshot.frame_index + 1}/{snapshot.frame_total}" if snapshot.frame_index is not None else "-"
    if snapshot.drag_pose_set and snapshot.drag_pose_index is not None:
        frame = f"force-point ({snapshot.drag_pose_set})"
    return [
        f"STATE   {snapshot.state}",
        f"ACTION  {snapshot.action_id or '-'}   FRAME {frame}",
        f"ROOT    ({snapshot.root_x:.1f}, {snapshot.root_y:.1f})",
        f"GRAB    ({snapshot.grab_dx:.1f}, {snapshot.grab_dy:.1f})",
        f"VEL     vx={snapshot.vx:.1f}  vy={snapshot.vy:.1f}  speed={snapshot.drag_speed:.2f}",
        f"DIR     {snapshot.direction or '-'}   BAND {snapshot.speed_band or '-'}",
        f"POSE    {snapshot.pose_set or '-'}",
        f"TILT    {snapshot.body_tilt:.2f} deg   hair {snapshot.hair_lag:.2f}   skirt {snapshot.skirt_lag:.2f}",
        f"FACE    {snapshot.expression}",
        f"GROUND  {snapshot.ground_y if snapshot.ground_y is not None else '-'}",
    ]


def draw_debug_overlay(painter: QPainter, rect: QRectF, snapshot: RenderSnapshot) -> None:
    lines = debug_lines(snapshot)
    width = max(215.0, 24.0 + max(len(line) for line in lines) * 7.0)
    height = 18.0 + len(lines) * 16.0
    panel = QRectF(rect.left() + 4.0, rect.top() + 4.0, width, height)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawRoundedRect(panel, 6.0, 6.0)
    font = QFont("Consolas")
    font.setPixelSize(12)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255))
    for index, line in enumerate(lines):
        painter.drawText(panel.left() + 8.0, panel.top() + 20.0 + index * 16.0, line)
    painter.restore()
