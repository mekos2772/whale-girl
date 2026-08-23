"""Best-effort diagnostics shared by domain and IO modules: %TEMP% file."""

from __future__ import annotations

import os
import time

DEBUG_LOG_PATH = os.path.join(os.environ.get("TEMP", "."), "mimi-pet-debug.log")


def dbg(msg: str) -> None:
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass
