from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActionSpec:
    id: str
    name_zh: str
    description: str
    frames: tuple[Path, ...]
    duration_ms: tuple[int, ...]
    loop: bool
    trigger: str
    exit_state: str
    canvas: tuple[int, int] = (512, 768)
    ground_pivot: tuple[int, int] = (256, 744)
    registration_scale: float = 1.0
    registration_scale_y: float | None = None

    @property
    def total_duration_ms(self) -> int:
        return sum(self.duration_ms)


def _parse_registration_scale(raw: object) -> tuple[float, float]:
    """Accept a single scale or an [sx, sy] pair (fixed, non-uniform registration)."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        sx = float(raw[0])
        sy = float(raw[1])
        return (sx, sy)
    value = float(raw) if raw is not None else 1.0
    return (value, value)


class ActionLibrary:
    """Strict allowlist loader; it never scans candidate or generation folders."""

    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        self.root = self.manifest_path.parent
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.live_rig = data["live_rig"]
        self._actions: dict[str, ActionSpec] = {}
        for raw in data["usable_actions"]:
            if raw.get("status") != "usable" or raw.get("enabled") is not True:
                continue
            frames = tuple((self.root / value).resolve() for value in raw["frames"])
            durations = tuple(int(value) for value in raw["duration_ms"])
            if len(frames) != len(durations) or not frames:
                raise ValueError(f"{raw['id']}: frame and duration counts differ")
            missing = [str(path) for path in frames if not path.is_file()]
            if missing:
                raise FileNotFoundError(f"{raw['id']}: missing frames: {missing[:3]}")
            if any(value <= 0 for value in durations):
                raise ValueError(f"{raw['id']}: durations must be positive")
            spec = ActionSpec(
                id=raw["id"],
                name_zh=raw["name_zh"],
                description=raw["description"],
                frames=frames,
                duration_ms=durations,
                loop=bool(raw["loop"]),
                trigger=raw["trigger"],
                exit_state=raw["exit_state"],
                canvas=tuple(int(value) for value in raw.get("canvas", [512, 768])),
                ground_pivot=tuple(int(value) for value in raw.get("ground_pivot", [256, 744])),
                registration_scale=_parse_registration_scale(raw.get("registration_scale", 1.0))[0],
                registration_scale_y=_parse_registration_scale(raw.get("registration_scale", 1.0))[1],
            )
            self._actions[spec.id] = spec
        self._drag_poses: dict[str, ActionSpec] = {}
        self._release_transitions: dict[str, ActionSpec] = {}
        drag_poses = data.get("drag_poses")
        if drag_poses:
            canvas = tuple(int(value) for value in drag_poses.get("canvas", [512, 768]))
            for set_id, raw in drag_poses.get("sets", {}).items():
                frames = tuple((self.root / value).resolve() for value in raw["frames"])
                durations = tuple(int(value) for value in raw["duration_ms"])
                if len(frames) != len(durations) or not frames:
                    raise ValueError(f"drag_poses.{set_id}: frame and duration counts differ")
                missing = [str(path) for path in frames if not path.is_file()]
                if missing:
                    raise FileNotFoundError(f"drag_poses.{set_id}: missing frames: {missing[:3]}")
                if any(value <= 0 for value in durations):
                    raise ValueError(f"drag_poses.{set_id}: durations must be positive")
                self._drag_poses[set_id] = ActionSpec(
                    id=set_id,
                    name_zh=set_id,
                    description=drag_poses.get("note", ""),
                    frames=frames,
                    duration_ms=durations,
                    loop=bool(raw.get("loop", True)),
                    trigger="drag_pose",
                    exit_state="idle",
                    canvas=canvas,
                    ground_pivot=(256, 744),
                    registration_scale=_parse_registration_scale(
                        raw.get("registration_scale", 1.0)
                    )[0],
                    registration_scale_y=_parse_registration_scale(
                        raw.get("registration_scale", 1.0)
                    )[1],
                )
            for transition_id, raw in drag_poses.get("release_transitions", {}).items():
                frames = tuple((self.root / value).resolve() for value in raw["frames"])
                durations = tuple(int(value) for value in raw["duration_ms"])
                if len(frames) != len(durations) or not frames:
                    raise ValueError(f"release_transitions.{transition_id}: counts differ")
                missing = [str(path) for path in frames if not path.is_file()]
                if missing:
                    raise FileNotFoundError(
                        f"release_transitions.{transition_id}: missing frames: {missing[:3]}"
                    )
                self._release_transitions[transition_id] = ActionSpec(
                    id=transition_id,
                    name_zh=f"{transition_id} release",
                    description="reverse v6 sheets: set-down back to standing",
                    frames=frames,
                    duration_ms=durations,
                    loop=False,
                    trigger="release_transition",
                    exit_state="idle",
                    canvas=canvas,
                    ground_pivot=(256, 744),
                    registration_scale=_parse_registration_scale(
                        raw.get("registration_scale", 1.0)
                    )[0],
                    registration_scale_y=_parse_registration_scale(
                        raw.get("registration_scale", 1.0)
                    )[1],
                )

    def get(self, action_id: str) -> ActionSpec:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise KeyError(f"Action is not in the usable allowlist: {action_id}") from exc

    def all(self) -> tuple[ActionSpec, ...]:
        return tuple(self._actions.values())

    def random_performances(self) -> tuple[ActionSpec, ...]:
        return tuple(action for action in self._actions.values() if action.trigger == "random_performance")

    def drag_pose(self, set_id: str) -> ActionSpec:
        """Hybrid drag pose loop (user-approved main poses; manifest only)."""
        try:
            return self._drag_poses[set_id]
        except KeyError as exc:
            raise KeyError(f"Drag pose set is not registered: {set_id}") from exc

    def drag_pose_sets(self) -> tuple[ActionSpec, ...]:
        return tuple(self._drag_poses.values())

    def release_transition(self, direction: str) -> ActionSpec:
        """Set-down animation: the v6 sheets played in reverse back to a
        near-standing pose before the rig idle takes over."""
        try:
            return self._release_transitions[direction]
        except KeyError as exc:
            raise KeyError(f"Release transition is not registered: {direction}") from exc
