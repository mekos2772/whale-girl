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
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_HOST = "127.0.0.1:3080"
EVENTS_PATH = "/api/events.mux"
RESPOND_PATH = "/api/respond"


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
        return self.rpc("session.prompt", {"sessionId": session_id, "text": text})

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
            except Exception:
                if self.on_disconnect is not None:
                    self.on_disconnect()
            if not self._stop.is_set():
                time.sleep(2.0)
