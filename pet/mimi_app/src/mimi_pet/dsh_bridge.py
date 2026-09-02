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
import urllib.request
from dataclasses import dataclass
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
        self._counter_lock = threading.Lock()
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
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network / json errors
            raise DshError(f"dsh {path} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise DshError(f"dsh {path} failed: invalid response envelope")
        return data

    def rpc(self, method: str, payload: dict[str, Any]) -> Any:
        # Polling, prompts and question replies can originate on different
        # threads. Keep rpc ids unique even outside CPython's GIL semantics.
        with self._counter_lock:
            self._counter += 1
            rpc_id = self._counter
        envelope = {
            "type": "client-request",
            "rpcId": f"mimi-{rpc_id}",
            "method": method,
            "payload": payload,
        }
        data = self._post(f"/api/{method}", envelope)
        result = data.get("result") or {}
        if not isinstance(result, dict):
            raise DshError(f"dsh rpc {method}: invalid result envelope")
        if not result.get("ok"):
            error = result.get("error") or {}
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
            else:
                code, message = "unknown", str(error)
            raise DshError(f"dsh rpc {method}: {code}: {message}")
        return result.get("value")

    # ------------------------------------------------------------------ API

    def list_sessions(self) -> list[DshSession]:
        value = self.rpc("session.list", {}) or {}
        if not isinstance(value, dict):
            raise DshError("dsh rpc session.list: invalid value")
        sessions = []
        for item in value.get("items", []):
            if not isinstance(item, dict) or not item.get("sessionId"):
                continue
            projections = item.get("projections") or {}
            if not isinstance(projections, dict):
                projections = {}
            values = projections.get("values") or {}
            if not isinstance(values, dict):
                values = {}
            stats = values.get("sessionStats") or {}
            if not isinstance(stats, dict):
                stats = {}
            try:
                turns = int(stats.get("turns", 0))
            except (TypeError, ValueError):
                turns = 0
            try:
                steps = int(stats.get("steps", 0))
            except (TypeError, ValueError):
                steps = 0
            sessions.append(
                DshSession(
                    session_id=str(item["sessionId"]),
                    running=bool(item.get("running")),
                    turns=turns,
                    steps=steps,
                    cwd=str(item.get("cwd") or ""),
                    agent_preset=str(item.get("agentPreset") or ""),
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
        if not isinstance(value, dict):
            raise DshError("dsh rpc settings.describe: invalid value")
        for ns in value.get("namespaces", []):
            if not isinstance(ns, dict) or ns.get("ns") != "permission":
                continue
            ns_value = ns.get("value") or {}
            if not isinstance(ns_value, dict):
                ns_value = {}
            preset = ns_value.get("defaultPreset") or "workspace-write"
            try:
                revision = int(ns.get("revision", 0))
            except (TypeError, ValueError):
                revision = 0
            return str(preset), revision
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
        if not isinstance(value, dict):
            raise DshError("dsh rpc settings.mutate: invalid value")
        for ns in value.get("namespaces", []):
            if not isinstance(ns, dict) or ns.get("ns") != "permission":
                continue
            try:
                return int(ns.get("revision", expected_revision + 1))
            except (TypeError, ValueError):
                return expected_revision + 1
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
        self._socket = None
        self._socket_lock = threading.Lock()
        self._connected = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dsh-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # recv() intentionally has no timeout; closing the socket is what
        # makes shutdown prompt instead of leaving a daemon reader behind.
        with self._socket_lock:
            ws = self._socket
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _set_connected(self, connected: bool) -> None:
        """Emit callbacks only on actual transitions, never per retry."""
        if connected == self._connected:
            return
        self._connected = connected
        callback = self.on_connect if connected else self.on_disconnect
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                dbg(f"ws callback error: {type(exc).__name__}: {exc}")

    def _run(self) -> None:
        import websocket  # local import: optional dependency

        retry_delay = 0.5
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(f"ws://{self.host}{EVENTS_PATH}", timeout=20)
                with self._socket_lock:
                    self._socket = ws
                # The 20 s timeout above is for the handshake only. recv()
                # must block indefinitely: DSH stays quiet for minutes, and a
                # recv timeout would kill an otherwise healthy link every
                # 20 s, swallowing live events during the reconnect gap.
                ws.settimeout(None)
                dbg(f"ws connected {self.host}")
                self._set_connected(True)
                retry_delay = 0.5
                while not self._stop.is_set():
                    raw = ws.recv()
                    if raw in (None, "", b""):
                        raise ConnectionError("event stream closed")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    frame = json.loads(raw)
                    if not isinstance(frame, dict) or frame.get("type") != "server-request":
                        continue
                    payload = frame.get("payload") or {}
                    self.events.put(
                        DshEvent(
                            method=str(frame.get("method", "")),
                            rpc_id=str(frame.get("rpcId", "")),
                            payload=payload if isinstance(payload, dict) else {},
                        )
                    )
            except Exception as exc:
                if not self._stop.is_set():
                    dbg(f"ws error: {type(exc).__name__}: {exc}")
            finally:
                with self._socket_lock:
                    if self._socket is ws:
                        self._socket = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._set_connected(False)
            if not self._stop.is_set():
                # Exponential backoff avoids a hot retry loop while still
                # recovering quickly when DSH finishes starting up.
                self._stop.wait(retry_delay)
                retry_delay = min(8.0, retry_delay * 2.0)
