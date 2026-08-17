from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .action_library import ActionLibrary
from .config import load_config
from .drag_controller import DragConfig, DragHybridController
from .state_machine import Event, PetStateMachine


def dry_run() -> None:
    config = load_config()
    library = ActionLibrary(Path(config["asset_manifest"]))
    state = PetStateMachine()
    state.dispatch(Event.PICK_UP)
    drag = DragHybridController(DragConfig(**config["drag"]))
    drag.reset(0, 0, 0.0)
    sample = drag.update(80, 4, 0.05)
    state.dispatch(Event.RELEASE_AIRBORNE)
    state.dispatch(Event.GROUND_COLLISION)
    landing = library.get("land_recover_v4_12")
    state.dispatch(Event.ACTION_FINISHED)
    print(f"usable_actions={len(library.all())}")
    print(f"random_performances={len(library.random_performances())}")
    print(f"drag_sample={sample.pose_set}, speed={sample.speed:.1f}px/s")
    print(f"landing={landing.id}, frames={len(landing.frames)}")
    print(f"final_state={state.state.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mimi desktop pet")
    parser.add_argument("--dry-run", action="store_true", help="validate assets and core controllers without a window")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
        return
    try:
        from .qt_app import run
    except ImportError as exc:
        print(
            "PySide6 is required for the desktop pet window.\n"
            "Install it with:  python -m venv .venv && .\\.venv\\Scripts\\python -m pip install \"PySide6>=6.7,<7\"\n"
            "then run:  .\\.venv\\Scripts\\python -m mimi_pet",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    raise SystemExit(run())


if __name__ == "__main__":
    main()
