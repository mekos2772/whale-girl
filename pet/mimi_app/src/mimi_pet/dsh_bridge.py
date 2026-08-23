"""DSH (DeepSeek Harness) integration bridge.

Connects the pet to the local DSH web API (default http://127.0.0.1:3080):
- RPC calls (POST /api/<method> with the client-request envelope) for status,
  prompts and question answers;
- a WebSocket thread on /api/events.mux delivering live frames (jobs, tool
  events, user questions, approvals) into a thread-safe queue.

Pure Python (urllib + websocket-client), no Qt import: unit-testable.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .debug_log import dbg  # noqa: F401 (re-exported for existing importers)

DEFAULT_HOST = "127.0.0.1:3080"
EVENTS_PATH = "/api/events.mux"
RESPOND_PATH = "/api/respond"


def _local_timezone() -> str:
    """IANA timezone for prompt payloads; Windows-safe fallback UTC+8."""
    try:
        import tzlocal

        name = tzlocal.get_localzone_name()
        if name:
            return name
    except Exception:
        pass
    return "Asia/Shanghai"


class DshError(RuntimeError):
    pass


@dataclass(frozen=True)
class DshSession:
    session_id: str
    running: bool = False
    turns: int = 0
    steps: int = 0
    cwd: str = ""
    agent_preset: str = ""
    title: str = ""


@dataclass
class DshEvent:
    """One decoded server-request frame from the event stream."""

    method: str
    rpc_id: str
    payload: dict[str, Any]


class DshBridge:
    """HTTP RPC client for the DSH web API."""

    def __init__(self, host: str = DEFAULT_HOST, timeout: float = 5.0) -> None:
        self.host = host
        self.timeout = timeout
        self._counter = 0
        self.timezone = os.environ.get("MIMI_TZ") or _local_timezone()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"http://{self.host}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network / json errors
            raise DshError(f"dsh {path} failed: {exc}") from exc

    def rpc(self, method: str, payload: dict[str, Any]) -> Any:
        self._counter += 1
        envelope = {
            "type": "client-request",
            "rpcId": f"mimi-{self._counter}",
            "method": method,
            "payload": payload,
        }
        data = self._post(f"/api/{method}", envelope)
        result = data.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            raise DshError(f"dsh rpc {method}: {error.get('code')}: {error.get('message')}")
        return result.get("value")

    # ------------------------------------------------------------------ API

    def list_sessions(self) -> list[DshSession]:
        value = self.rpc("session.list", {}) or {}
        sessions = []
        for item in value.get("items", []):
            values = (item.get("projections") or {}).get("values") or {}
            stats = values.get("sessionStats") or {}
            sessions.append(
                DshSession(
                    session_id=item["sessionId"],
                    running=bool(item.get("running")),
                    turns=int(stats.get("turns", 0)),
                    steps=int(stats.get("steps", 0)),
                    cwd=item.get("cwd", ""),
                    agent_preset=item.get("agentPreset", ""),
                    title=str(values.get("title") or ""),
                )
            )
        return sessions

    def prompt(self, session_id: str, text: str) -> Any:
        # Wire schema (dsh-client-connection): mode is "queue" for a new
        # turn (or "steer" mid-turn); content is an array of typed parts.
        payload = {
            "sessionId": session_id,
            "mode": "queue",
            "content": [{"type": "text", "text": text}],
            "clientTimeZone": self.timezone,
        }
        return self.rpc("session.prompt", payload)

    # ------------------------------------------------------- shadow-session RPCs
    # Used by the pet-agent mode: the pet owns one archived session on the
    # DSH host (invisible in the web UI) driven by the same model config.

    def create_session(self) -> str:
        """Create a fresh session; returns its sessionId."""
        value = self.rpc("session.create", {}) or {}
        # Response shape is defensive: probe-tested live, but stay robust.
        if isinstance(value, str):
            return value
        for key in ("sessionId", "session_id", "id"):
            if isinstance(value, dict) and value.get(key):
                return str(value[key])
        raise DshError("dsh session.create: no sessionId in response")

    def archive_session(self, session_id: str) -> Any:
        """Hide the session from every web UI view (presentation-only)."""
        return self.rpc("workspace.archiveSession", {"sessionId": session_id})

    def rename_session(self, session_id: str, title: str) -> Any:
        return self.rpc("session.rename", {"sessionId": session_id, "title": title})

    def cancel_session(self, session_id: str) -> Any:
        return self.rpc("session.cancel", {"sessionId": session_id})

    # ------------------------------------------------------- settings RPCs
    # The permission preset of an EXISTING session cannot be changed over the
    # HTTP RPC surface (the web UI runs /permission through a WS remote), but
    # settings.mutate controls the DEFAULT preset applied to new sessions —
    # so the pet flips the default, creates its session, and restores it.

    def settings_permission_preset(self) -> tuple[str, int]:
        """(default preset, revision) of the "permission" settings namespace."""
        value = self.rpc("settings.describe", {}) or {}
        for ns in value.get("namespaces", []):
            if ns.get("ns") == "permission":
                preset = (ns.get("value") or {}).get("defaultPreset") or "workspace-write"
                return str(preset), int(ns.get("revision", 0))
        return "workspace-write", 0

    def settings_set_permission_preset(self, preset: str, expected_revision: int) -> int:
        """Set the default preset; returns the namespace's new revision."""
        value = self.rpc(
            "settings.mutate",
            {
                "ns": "permission",
                "ops": [{"op": "set", "path": ["defaultPreset"], "value": preset}],
                "expectedRevision": expected_revision,
            },
        ) or {}
        for ns in value.get("namespaces", []):
            if ns.get("ns") == "permission":
                return int(ns.get("revision", expected_revision + 1))
        return expected_revision + 1

    def respond(self, rpc_id: str, session_id: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """Answer a pending user question (client-response on /api/respond)."""
        body = {
            "type": "client-response",
            "rpcId": rpc_id,
            "result": {
                "ok": True,
                "value": {
                    "sessionId": session_id,
                    "answer": {"answers": answers},
                },
            },
        }
        data = self._post(RESPOND_PATH, body)
        return data


class DshEventThread:
    """Background WebSocket reader; decoded frames land in a thread-safe queue.

    Reconnects automatically. The queue is drained by the Qt side (a QTimer)
    so no Qt object is touched from this thread.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        events: queue.Queue[DshEvent] | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.events = events if events is not None else queue.Queue()
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dsh-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import websocket  # local import: optional dependency

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(f"ws://{self.host}{EVENTS_PATH}", timeout=20)
                # The 20 s timeout above is for the handshake only. recv()
                # must block indefinitely: DSH stays quiet for minutes, and a
                # recv timeout would kill an otherwise healthy link every
                # 20 s, swallowing live events during the reconnect gap.
                ws.settimeout(None)
                dbg(f"ws connected {self.host}")
                if self.on_connect is not None:
                    self.on_connect()
                while not self._stop.is_set():
                    raw = ws.recv()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    frame = json.loads(raw)
                    if frame.get("type") != "server-request":
                        continue
                    self.events.put(
                        DshEvent(
                            method=str(frame.get("method", "")),
                            rpc_id=str(frame.get("rpcId", "")),
                            payload=frame.get("payload") or {},
                        )
                    )
            except Exception as exc:
                dbg(f"ws error: {type(exc).__name__}: {exc}")
                if self.on_disconnect is not None:
                    self.on_disconnect()
            if not self._stop.is_set():
                time.sleep(2.0)
