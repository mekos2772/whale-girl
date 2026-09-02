from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = WORKSPACE_ROOT / "mimi_app" / "config" / "app.json"


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # The DSH plugin exposes a scale percentage. Apply it before the engine is
    # constructed so renderer, hit testing and physics all share one size.
    raw_scale = os.environ.get("MIMI_SCALE_PERCENT")
    if raw_scale:
        try:
            scale = max(1.0, min(200.0, float(raw_scale))) / 100.0
            window = data["window"]
            window["width"] = max(1, round(float(window["width"]) * scale))
            window["height"] = max(1, round(float(window["height"]) * scale))
        except (KeyError, TypeError, ValueError):
            pass
    manifest = Path(data["asset_manifest"])
    if not manifest.is_absolute():
        # 素材根可被环境变量覆盖（插件随包分发本体时指向外部素材目录）。
        asset_root = Path(os.environ.get("MIMI_ASSET_ROOT", WORKSPACE_ROOT))
        manifest = asset_root / manifest
    data["asset_manifest"] = str(manifest.resolve())
    return data
