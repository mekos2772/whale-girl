"""Rig model loader for the layered (Live-like) rig.

Parses ``live_rig/model.json`` (identical to ``rig_v3/model.json``) and
resolves every layer PNG to an absolute path. Purely declarative: no Qt, no
rendering. The renderer consumes the resulting RigModel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RigLayer:
    name: str
    file: Path
    pivot: tuple[float, float]
    z: int
    base_scale: float = 1.0
    base_offset: tuple[float, float] = (0.0, 0.0)
    switches: tuple[str, ...] = ()
    experimental: bool = False


@dataclass(frozen=True)
class EyeTrackingSpec:
    enabled: bool
    base_file: Path
    irises_file: Path
    clip_file: Path
    foreground_file: Path
    max_offset: tuple[float, float]
    source_parameters: tuple[str, str]
    supported_expressions: tuple[str, ...]


@dataclass(frozen=True)
class ExpressionPatches:
    """Region overlays that let expression states COMPOSE on a flat rig.

    The whole-master swap forces one winner per tick (a random blink erased an
    active smile; only "neutral" got the tracked irises). With patches the
    renderer draws the tracked neutral base, then the closed-eyelid patch and
    finally the mouth patch, so blink+smile/talk coexist and the eyes never
    snap between states.
    """

    mouth_happy: Path
    mouth_talk: Path
    lids_blink: Path


@dataclass(frozen=True)
class RigModel:
    name: str
    canvas: tuple[int, int]
    layers: tuple[RigLayer, ...]
    expressions: dict[str, str]
    parameters: dict[str, dict[str, float]] = field(default_factory=dict)
    eye_tracking: EyeTrackingSpec | None = None
    expression_patches: ExpressionPatches | None = None

    def layer(self, name: str) -> RigLayer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(f"rig has no layer named {name!r}")


def load_rig_model(model_path: Path) -> RigModel:
    """Load and validate a rig model.json; missing layer files raise FileNotFoundError."""
    model_path = model_path.resolve()
    data = json.loads(model_path.read_text(encoding="utf-8"))
    root = model_path.parent
    canvas = tuple(int(value) for value in data["canvas"])
    layers: list[RigLayer] = []
    for raw in data["layers"]:
        file = (root / raw["file"]).resolve()
        if not file.is_file():
            raise FileNotFoundError(f"rig layer {raw['name']}: missing {file}")
        layers.append(
            RigLayer(
                name=raw["name"],
                file=file,
                pivot=tuple(float(value) for value in raw["pivot"]),
                z=int(raw["z"]),
                base_scale=float(raw.get("base_scale", 1.0)),
                base_offset=tuple(float(value) for value in raw.get("base_offset", (0.0, 0.0))),
                switches=tuple(raw.get("switches", ())),
                experimental=bool(raw.get("experimental", False)),
            )
        )
    layers.sort(key=lambda layer: layer.z)
    expressions: dict[str, str] = {}
    for name, mapping in data.get("expressions", {}).items():
        if "head_expression" in mapping:
            # Layered rig: the expression swaps the head layer's file.
            expressions[name] = str(mapping["head_expression"])
        elif "character_master" in mapping:
            # Flat rig (v5): the expression swaps the whole master image
            # (blink/smile variants are edits of the same master).
            path = (root / str(mapping["character_master"])).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"rig expression {name}: missing {path}")
            expressions[name] = str(mapping["character_master"])
    parameters = data.get("parameters", {})
    raw_eye = data.get("eye_tracking")
    eye_tracking = None
    if raw_eye and raw_eye.get("enabled", False):
        def eye_file(key: str) -> Path:
            path = (root / raw_eye[key]).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"rig eye tracking {key}: missing {path}")
            return path

        max_offset = tuple(float(value) for value in raw_eye.get("max_offset", (8, 4)))
        source_parameters = tuple(raw_eye.get("source_parameters", ("head_x", "head_y")))
        if len(max_offset) != 2 or len(source_parameters) != 2:
            raise ValueError("eye_tracking max_offset and source_parameters must each have two values")
        eye_tracking = EyeTrackingSpec(
            enabled=True,
            base_file=eye_file("base_file"),
            irises_file=eye_file("irises_file"),
            clip_file=eye_file("clip_file"),
            foreground_file=eye_file("foreground_file"),
            max_offset=max_offset,  # type: ignore[arg-type]
            source_parameters=source_parameters,  # type: ignore[arg-type]
            supported_expressions=tuple(raw_eye.get("supported_expressions", ("neutral",))),
        )
    expression_patches: ExpressionPatches | None = None
    raw_patches = data.get("expression_patches")
    if raw_patches:
        def patch_file(key: str) -> Path:
            path = (root / str(raw_patches[key])).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"rig expression patch {key}: missing {path}")
            return path

        expression_patches = ExpressionPatches(
            mouth_happy=patch_file("mouth_happy"),
            mouth_talk=patch_file("mouth_talk"),
            lids_blink=patch_file("lids_blink"),
        )
    return RigModel(
        name=str(data.get("name", "Mimi rig")),
        canvas=canvas,  # type: ignore[arg-type]
        layers=tuple(layers),
        expressions=expressions,
        parameters=parameters,
        eye_tracking=eye_tracking,
        expression_patches=expression_patches,
    )
