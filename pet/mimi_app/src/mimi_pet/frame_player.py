from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .action_library import ActionSpec


@dataclass(frozen=True)
class FrameSample:
    path: Path
    index: int
    finished: bool


class FramePlayer:
    """Time-based player that preserves every authored frame duration."""

    def __init__(self) -> None:
        self.action: ActionSpec | None = None
        self.elapsed_ms = 0.0

    def play(self, action: ActionSpec) -> None:
        self.action = action
        self.elapsed_ms = 0.0

    def stop(self) -> None:
        self.action = None
        self.elapsed_ms = 0.0

    def advance(self, delta_ms: float) -> FrameSample | None:
        action = self.action
        if action is None:
            return None
        self.elapsed_ms += max(0.0, delta_ms)
        total = action.total_duration_ms
        finished = False
        if action.loop:
            self.elapsed_ms %= total
        elif self.elapsed_ms >= total:
            self.elapsed_ms = float(total)
            finished = True

        cursor = 0
        index = len(action.frames) - 1
        for candidate, duration in enumerate(action.duration_ms):
            cursor += duration
            if self.elapsed_ms < cursor:
                index = candidate
                break
        return FrameSample(action.frames[index], index, finished)
