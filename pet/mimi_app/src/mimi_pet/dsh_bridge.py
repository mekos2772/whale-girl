"""DSH (DeepSeek Harness) integration bridge.

DSH 0.1.2 uses the Remote protocol:

* unary calls are POST ``/api/<namespace>/<method>`` with ``payload.args``;
* all logical streams share the ``/api/remote.mux`` WebSocket;
* ``$events`` carries forwarded status/question/approval events;
* ``session/control`` carries jobs and ``session/follow`` carries durable
  session events.

The Qt side intentionally keeps its small, legacy ``DshEvent`` vocabulary.
This module is the protocol adapter between that vocabulary and DSH Remote.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Callable

from .debug_log import dbg  # noqa: F401 (re-exported for existing importers)

DEFAULT_HOST = os.environ.get("MIMI_DSH_HOST", "127.0.0.1:3080")
REMOTE_PATH = "/api/remote.mux"
# Public compatibility alias used by older diagnostics/importers.
EVENTS_PATH = REMOTE_PATH
RESPOND_PATH = "/api/$events/result"


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
    model_provider: str = ""
    model_id: str = ""
    reasoning_effort: str = ""


@dataclass(frozen=True)
class ModelReasoningEffort:
    effort_id: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class ModelCatalogEntry:
    """One model route exposed by DSH's model catalog RPC."""

    model_id: str
    label: str = ""
    provider: str = ""
    provider_label: str = ""
    description: str = ""
    reasoning_efforts: tuple[ModelReasoningEffort, ...] = ()
    default_reasoning_effort: str = ""


@dataclass
class DshEvent:
    """One normalized event delivered to the Qt-side integration.

    ``rpc_id`` remains the legacy single-correlation alias. New waterfall
    requests use ``client_id`` and ``event_id``; ``seq`` is used for duplicate
    suppression when a follow stream reconnects with a baseline replay.
    """

    method: str
    rpc_id: str
    payload: dict[str, Any]
    client_id: str = ""
    event_id: str = ""
    seq: int | None = None


class _DshAuth:
    """Exchange a DSH launch URL for an in-memory signed session cookie.

    The URL is supplied by the Node plugin only through the child environment.
    It is never logged or persisted. Old DSH installations without a URL still
    work because the cookie jar simply remains empty.
    """

    def __init__(self, auth_url: str, timeout: float) -> None:
        self.auth_url = str(auth_url or "").strip()
        self.timeout = timeout
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._lock = threading.Lock()
        self._authenticated = False

    def ensure(self) -> None:
        if not self.auth_url or self._authenticated:
            return
        with self._lock:
            if self._authenticated:
                return
            request = urllib.request.Request(
                self.auth_url,
                headers={"Accept": "text/html"},
                method="GET",
            )
            try:
                # urllib follows DSH's 303 redirect and stores Set-Cookie in
                # the CookieJar before returning the clean root document.
                with self.opener.open(request, timeout=self.timeout) as response:
                    response.read(1)
            except Exception as exc:
                raise DshError(f"dsh authentication failed: {exc}") from exc
            self._authenticated = True

    def invalidate(self) -> None:
        with self._lock:
            self._authenticated = False

    def cookie_header(self, host: str) -> str:
        self.ensure()
        request = urllib.request.Request(f"http://{host}/")
        self.jar.add_cookie_header(request)
        return request.get_header("Cookie") or ""


class DshBridge:
    """HTTP Remote RPC client for the DSH web API."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: float = 5.0,
        auth_url: str | None = None,
    ) -> None:
        self.host = host
        self.timeout = timeout
        self._counter = 0
        self._counter_lock = threading.Lock()
        self.timezone = os.environ.get("MIMI_TZ") or _local_timezone()
        self.auth_url = auth_url if auth_url is not None else os.environ.get("MIMI_DSH_AUTH_URL", "")
        self._auth = _DshAuth(self.auth_url, timeout)

    @property
    def auth(self) -> _DshAuth:
        """Shared auth helper; the event thread must reuse this instance."""
        return self._auth

    @staticmethod
    def _endpoint(method: str) -> str:
        """Normalize old dotted calls without putting them on the wire."""
        method = str(method)
        if "." in method and "/" not in method:
            return method.replace(".", "/")
        return method

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._auth.ensure()
        for attempt in range(2):
            request = urllib.request.Request(
                f"http://{self.host}{path}",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with self._auth.opener.open(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and self.auth_url and attempt == 0:
                    self._auth.invalidate()
                    self._auth.ensure()
                    continue
                raise DshError(f"dsh {path} failed: HTTP {exc.code}") from exc
            except Exception as exc:  # network / JSON errors
                raise DshError(f"dsh {path} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise DshError(f"dsh {path} failed: invalid response envelope")
        return data

    def rpc(self, method: str, payload: dict[str, Any]) -> Any:
        """Call a unary Remote method using the strict 0.1.2 envelope."""
        with self._counter_lock:
            self._counter += 1
            rpc_id = self._counter
        endpoint = self._endpoint(method)
        envelope = {
            "type": "client-request",
            "rpcId": f"mimi-{rpc_id}",
            "method": endpoint,
            "payload": {"args": payload},
        }
        data = self._post(f"/api/{endpoint}", envelope)
        if data.get("type") != "server-response":
            raise DshError(f"dsh rpc {endpoint}: invalid response type")
        if data.get("rpcId") != envelope["rpcId"]:
            raise DshError(f"dsh rpc {endpoint}: response rpcId mismatch")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DshError(f"dsh rpc {endpoint}: invalid result envelope")
        if not result.get("ok"):
            error = result.get("error") or {}
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
            else:
                code, message = "unknown", str(error)
            raise DshError(f"dsh rpc {endpoint}: {code}: {message}")
        return result.get("value")

    # ------------------------------------------------------------------ API

    def list_sessions(self) -> list[DshSession]:
        value = self.rpc("session/list", {"_request": {}}) or {}
        if not isinstance(value, dict):
            raise DshError("dsh rpc session/list: invalid value")
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
            model_selection = values.get("modelSelection") or {}
            if not isinstance(model_selection, dict):
                model_selection = {}
            selected_model = model_selection.get("next") or model_selection.get("lastUsed") or {}
            if not isinstance(selected_model, dict):
                selected_model = {}
            sessions.append(
                DshSession(
                    session_id=str(item["sessionId"]),
                    running=bool(item.get("running")),
                    turns=turns,
                    steps=steps,
                    cwd=str(item.get("cwd") or ""),
                    agent_preset=str(item.get("agentPreset") or values.get("agentPreset") or ""),
                    title=str(values.get("title") or ""),
                    model_provider=str(selected_model.get("provider") or ""),
                    model_id=str(selected_model.get("model") or ""),
                    reasoning_effort=str(selected_model.get("reasoningEffort") or ""),
                )
            )
        return sessions

    def prompt(self, session_id: str, text: str) -> Any:
        payload = {
            "request": {
                "requestId": f"mimi-prompt-{uuid.uuid4().hex}",
                "sessionId": session_id,
                "mode": "queue",
                "content": [{"type": "text", "text": text}],
                "clientTimeZone": self.timezone,
            }
        }
        return self.rpc("session/prompt", payload)

    # ------------------------------------------------------- shadow-session RPCs

    def create_session(self) -> str:
        """Create a fresh session; returns its sessionId."""
        value = self.rpc("session/create", {"request": {}}) or {}
        if isinstance(value, str):
            return value
        for key in ("sessionId", "session_id", "id"):
            if isinstance(value, dict) and value.get(key):
                return str(value[key])
        raise DshError("dsh session/create: no sessionId in response")

    def archive_session(self, session_id: str) -> Any:
        return self.rpc("workspace/archiveSession", {"request": {"sessionId": session_id}})

    def rename_session(self, session_id: str, title: str) -> Any:
        return self.rpc("session/rename", {"request": {"sessionId": session_id, "title": title}})

    def cancel_session(self, session_id: str) -> Any:
        return self.rpc("session/cancel", {"request": {"sessionId": session_id}})

    # ---------------------------------------------------------- model catalog RPCs

    def model_catalog(self) -> list[ModelCatalogEntry]:
        """Return the selectable provider/model routes reported by DSH."""
        value = self.rpc("session/modelCatalog", {}) or {}
        if not isinstance(value, dict):
            raise DshError("dsh rpc session/modelCatalog: invalid value")
        out: list[ModelCatalogEntry] = []
        groups = value.get("groups") or []
        if not isinstance(groups, list):
            raise DshError("dsh rpc session/modelCatalog: invalid groups")
        for group in groups:
            if not isinstance(group, dict):
                continue
            provider = str(group.get("id") or "").strip()
            if not provider:
                continue
            provider_label = str(group.get("name") or provider)
            models = group.get("models") or []
            if not isinstance(models, list):
                continue
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = str(model.get("id") or "").strip()
                if not model_id:
                    continue
                reasoning = model.get("reasoning") or {}
                if not isinstance(reasoning, dict):
                    reasoning = {}
                efforts: list[ModelReasoningEffort] = []
                for effort in reasoning.get("efforts") or []:
                    if not isinstance(effort, dict):
                        continue
                    effort_id = str(effort.get("id") or "").strip()
                    if not effort_id:
                        continue
                    efforts.append(
                        ModelReasoningEffort(
                            effort_id=effort_id,
                            label=str(effort.get("name") or effort_id),
                            description=str(effort.get("description") or ""),
                        )
                    )
                out.append(
                    ModelCatalogEntry(
                        model_id=model_id,
                        label=str(model.get("name") or model_id),
                        provider=provider,
                        provider_label=provider_label,
                        description=str(model.get("description") or ""),
                        reasoning_efforts=tuple(efforts),
                        default_reasoning_effort=str(reasoning.get("defaultEffort") or ""),
                    )
                )
        return out

    def select_session_model(
        self,
        session_id: str,
        model_id: str,
        provider: str,
        reasoning_effort: str | None = None,
    ) -> Any:
        """Switch the provider/model route used by the session's next prompt."""
        model_id = (model_id or "").strip()
        provider = (provider or "").strip()
        if not model_id or not provider:
            return {"noop": True}
        request: dict[str, Any] = {
            "sessionId": session_id,
            "provider": provider,
            "model": model_id,
        }
        if reasoning_effort:
            request["reasoningEffort"] = reasoning_effort
        return self.rpc("session/selectModel", {"request": request})

    # ------------------------------------------------------- settings RPCs

    def settings_permission_preset(self) -> tuple[str, int]:
        """Return ``(defaultPreset, revision)`` for the permission namespace."""
        value = self.rpc("settings/describe", {}) or {}
        if not isinstance(value, dict):
            raise DshError("dsh rpc settings/describe: invalid value")
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
        """Set the default preset; 0.1.2 returns one namespace view directly."""
        value = self.rpc(
            "settings/mutate",
            {
                "ns": "permission",
                "ops": [{"op": "set", "path": ["defaultPreset"], "value": preset}],
                "expectedRevision": expected_revision,
            },
        ) or {}
        if not isinstance(value, dict):
            raise DshError("dsh rpc settings/mutate: invalid value")
        try:
            return int(value.get("revision", expected_revision + 1))
        except (TypeError, ValueError):
            return expected_revision + 1

    def respond(self, client_id: str, event_id: str, outcome: Any) -> Any:
        """Settle a forwarded Remote Event request via ``$events/result``.

        ``outcome`` is normally ``{"answers": [...]}`` for a question or a
        string such as ``"allowed-once"`` for approval. A list is accepted as
        a compatibility shorthand for the old question API.
        """
        if isinstance(outcome, list):
            outcome = {"answers": outcome}
        return self.rpc(
            "$events/result",
            {
                "clientId": client_id,
                "eventId": event_id,
                "outcome": {"kind": "result", "value": outcome},
            },
        )

    def respond_approval(self, client_id: str, event_id: str, outcome: str) -> Any:
        if outcome not in {"allowed-once", "rejected", "cancelled", "unavailable"}:
            raise ValueError(f"unsupported approval outcome: {outcome}")
        return self.respond(client_id, event_id, outcome)


class DshEventThread:
    """Background Remote-mux reader; never touches Qt objects."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        events: queue.Queue[DshEvent] | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        auth_url: str | None = None,
        auth: _DshAuth | None = None,
    ) -> None:
        self.host = host
        self.events = events if events is not None else queue.Queue()
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.auth_url = auth_url if auth_url is not None else os.environ.get("MIMI_DSH_AUTH_URL", "")
        self._auth = auth or _DshAuth(self.auth_url, 20.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._socket_lock = threading.Lock()
        self._connected = False
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._desired_sessions: set[str] = set()
        self._client_id = ""
        self._seen_sequences: dict[str, set[int]] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dsh-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
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

    def set_sessions(self, session_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        desired = {str(sid) for sid in session_ids if sid}
        self._desired_sessions = desired
        self._commands.put(("sessions", desired))

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        callback = self.on_connect if connected else self.on_disconnect
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                dbg(f"ws callback error: {type(exc).__name__}: {exc}")

    @staticmethod
    def _is_timeout(exc: Exception, websocket_module: Any) -> bool:
        timeout_type = getattr(websocket_module, "WebSocketTimeoutException", None)
        return timeout_type is not None and isinstance(exc, timeout_type)

    @staticmethod
    def _send_json(ws: Any, value: dict[str, Any]) -> None:
        ws.send(json.dumps(value, ensure_ascii=False))

    def _send_open(self, ws: Any, stream_id: str, endpoint: str, args: dict[str, Any]) -> None:
        self._send_json(
            ws,
            {
                "type": "open",
                "streamId": stream_id,
                "endpoint": endpoint,
                "payload": {"args": args},
            },
        )

    def _open_follow_streams(self, ws: Any, active: dict[str, str], desired: set[str]) -> None:
        open_sessions = {
            session_id: stream_id
            for stream_id, session_id in active.items()
            if stream_id.startswith("follow:")
        }
        for session_id in sorted(desired - set(open_sessions)):
            stream_id = f"follow:{session_id}"
            self._send_open(
                ws,
                stream_id,
                "session/follow",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": session_id},
                        "maxMessages": 100,
                    }
                },
            )
            active[stream_id] = session_id
        for session_id, stream_id in list(open_sessions.items()):
            if session_id not in desired:
                try:
                    self._send_json(ws, {"type": "cancel", "streamId": stream_id})
                except Exception:
                    pass
                active.pop(stream_id, None)

    def _run_legacy_socket(self, ws: Any) -> None:
        """Keep pre-Remote unit fakes and old embedders from spinning forever."""
        self._set_connected(True)
        while not self._stop.is_set():
            raw = ws.recv()
            if raw in (None, "", b""):
                raise ConnectionError("legacy event socket closed")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "server-request":
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                self._put(
                    str(frame.get("method") or ""),
                    payload,
                    rpc_id=str(frame.get("rpcId") or ""),
                )

    def _run(self) -> None:
        import websocket  # local import: optional dependency

        retry_delay = 0.5
        while not self._stop.is_set():
            ws = None
            active: dict[str, str] = {}
            try:
                cookie = self._auth.cookie_header(self.host)
                kwargs: dict[str, Any] = {"timeout": 20}
                if cookie:
                    kwargs["cookie"] = cookie
                ws = websocket.create_connection(f"ws://{self.host}{REMOTE_PATH}", **kwargs)
                with self._socket_lock:
                    self._socket = ws
                # A short timeout allows set_sessions() commands to be applied
                # while the server remains idle for minutes.
                ws.settimeout(0.5)
                if not callable(getattr(ws, "send", None)):
                    # Older embedders supplied a read-only fake/adapter for
                    # the pre-0.1.2 event socket. Preserve that test seam.
                    self._run_legacy_socket(ws)
                    continue
                self._send_open(ws, "events", "$events", {})
                self._send_open(ws, "control", "session/control", {})
                active["events"] = ""
                active["control"] = ""
                self._open_follow_streams(ws, active, self._desired_sessions)
                dbg(f"remote mux connected {self.host}")
                retry_delay = 0.5
                while not self._stop.is_set():
                    while True:
                        try:
                            command, value = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        if command == "sessions":
                            self._open_follow_streams(ws, active, set(value))
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        if self._is_timeout(exc, websocket):
                            continue
                        raise
                    if raw in (None, "", b""):
                        raise ConnectionError("remote mux closed")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        continue
                    frame_type = frame.get("type")
                    stream_id = str(frame.get("streamId", ""))
                    if frame_type == "item":
                        value = frame.get("value")
                        if stream_id == "events":
                            if isinstance(value, dict) and value.get("type") == "ready":
                                self._client_id = str(value.get("clientId") or "")
                                if self._client_id:
                                    # Ready, not the physical WebSocket
                                    # handshake, proves the event source is live.
                                    self._set_connected(True)
                            else:
                                self._decode_event_value(value)
                        elif stream_id == "control":
                            self._decode_control_value(value)
                        elif stream_id.startswith("follow:"):
                            self._decode_follow_value(stream_id.removeprefix("follow:"), value)
                    elif frame_type == "error":
                        error = frame.get("error") or {}
                        message = str(error.get("message") or "remote stream failed")
                        if stream_id == "events":
                            raise ConnectionError(message)
                        dbg(f"remote mux stream error stream={stream_id}: {message}")
                    elif frame_type == "end":
                        if stream_id == "events":
                            raise ConnectionError("remote events stream ended")
                        active.pop(stream_id, None)
                        if stream_id == "control" and not self._stop.is_set():
                            self._send_open(ws, "control", "session/control", {})
                            active["control"] = ""
                        elif stream_id.startswith("follow:") and stream_id.removeprefix("follow:") in self._desired_sessions:
                            self._open_follow_streams(ws, active, self._desired_sessions)
            except Exception as exc:
                if not self._stop.is_set():
                    dbg(f"remote mux error: {type(exc).__name__}: {exc}")
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
                self._stop.wait(retry_delay)
                retry_delay = min(8.0, retry_delay * 2.0)

    # -------------------------------------------------------------- decoders

    def _put(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        rpc_id: str = "",
        client_id: str = "",
        event_id: str = "",
        seq: int | None = None,
    ) -> None:
        self.events.put(
            DshEvent(
                method=method,
                rpc_id=rpc_id,
                payload=payload,
                client_id=client_id,
                event_id=event_id,
                seq=seq,
            )
        )

    def _decode_event_value(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        event_type = str(value.get("type") or "")
        if event_type == "emit":
            name = str(value.get("event") or "")
            args = value.get("args") or []
            if name == "api-session/status" and len(args) >= 2:
                self._put("session/status", {"sessionId": str(args[0]), "running": bool(args[1])})
            elif name == "api-session/activity" and args:
                self._put("session/activity", {"sessionId": str(args[0]), "updatedAt": args[1] if len(args) > 1 else 0})
            elif name == "api-session/error" and args:
                self._put("session/error", {"sessionId": str(args[0]), "message": str(args[1] if len(args) > 1 else "")})
            elif name == "api-session/added" and args and isinstance(args[0], dict):
                self._put("session/added", {"session": args[0]})
            elif name == "api-session/removed" and args:
                self._put("session/removed", {"sessionId": str(args[0])})
            return
        if event_type == "cancel":
            event_id = str(value.get("eventId") or "")
            if event_id:
                self._put(
                    "question/resolved",
                    {"eventId": event_id},
                    rpc_id=event_id,
                    client_id=self._client_id,
                    event_id=event_id,
                )
            return
        if event_type != "waterfall":
            return
        name = str(value.get("event") or "")
        event_id = str(value.get("eventId") or "")
        agent_id = str(value.get("agentId") or "")
        request = value.get("request") or {}
        if not isinstance(request, dict):
            request = {}
        payload = dict(request)
        # The Gateway projects Agent identity outside request. Mimi's existing
        # routing code uses sessionId, so normalize the identity here.
        payload.setdefault("sessionId", agent_id)
        if name == "user-questions/request":
            self._put(
                "question/requested",
                payload,
                rpc_id=event_id,
                client_id=self._client_id,
                event_id=event_id,
            )
        elif name == "approval/request":
            self._put(
                "approval/requested",
                payload,
                rpc_id=event_id,
                client_id=self._client_id,
                event_id=event_id,
            )

    def _decode_control_value(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        frame_type = value.get("type")
        if frame_type == "baseline":
            baseline = value.get("value") or {}
            jobs = baseline.get("jobs") if isinstance(baseline, dict) else {}
            if isinstance(jobs, dict):
                for session_id, rows in jobs.items():
                    self._put("session/jobs", {"sessionId": str(session_id), "jobs": rows if isinstance(rows, list) else []})
        elif frame_type == "jobs":
            self._put(
                "session/jobs",
                {
                    "sessionId": str(value.get("sessionId") or ""),
                    "jobs": value.get("jobs") if isinstance(value.get("jobs"), list) else [],
                },
            )

    def _remember_seq(self, session_id: str, seq: Any) -> bool:
        try:
            number = int(seq)
        except (TypeError, ValueError):
            return True
        seen = self._seen_sequences.setdefault(session_id, set())
        if number in seen:
            return False
        seen.add(number)
        if len(seen) > 4096:
            floor = max(seen) - 2048
            seen.intersection_update({item for item in seen if item >= floor})
        return True

    def _decode_follow_value(self, session_id: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        if value.get("type") == "snapshot":
            records = value.get("records") or []
        elif value.get("type") == "event" and isinstance(value.get("event"), dict):
            records = [value]
        else:
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("type") != "event":
                if record.get("type") == "chunks":
                    self._decode_chunk_row(session_id, record.get("event") or {})
                continue
            event = record.get("event") or {}
            if not isinstance(event, dict) or not self._remember_seq(session_id, event.get("seq")):
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}
            self._put(
                "session/event",
                {
                    "sessionId": session_id,
                    "event": {"type": str(event.get("type") or ""), "data": data},
                },
                seq=event.get("seq"),
            )

    def _decode_chunk_row(self, session_id: str, event: Any) -> None:
        if not isinstance(event, dict):
            return
        row_type = str(event.get("type") or "")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return
        try:
            first_seq = int(event.get("seq"))
        except (TypeError, ValueError):
            first_seq = None
        turn = data.get("turn", 0)
        step = data.get("step", 0)
        if row_type in ("chunkrow/text-chunks", "chunkrow/reasoning-chunks"):
            texts = data.get("texts") or []
            chunk_type = "text-delta" if row_type.endswith("text-chunks") else "reasoning-delta"
            for index, text in enumerate(texts):
                seq = first_seq + index if first_seq is not None else None
                if seq is not None and not self._remember_seq(session_id, seq):
                    continue
                self._put(
                    "session/event",
                    {
                        "sessionId": session_id,
                        "event": {
                            "type": "assistant/chunk",
                            "data": {
                                "turn": turn,
                                "step": step,
                                "chunk": {"type": chunk_type, "text": str(text)},
                            },
                        },
                    },
                    seq=seq,
                )
        elif row_type == "chunkrow/tool-call-chunks":
            args = data.get("args") or []
            name = str(data.get("name") or "")
            if name:
                seq = first_seq
                if seq is None or self._remember_seq(session_id, seq):
                    self._put(
                        "session/event",
                        {
                            "sessionId": session_id,
                            "event": {
                                "type": "assistant/chunk",
                                "data": {
                                    "turn": turn,
                                    "step": step,
                                    "chunk": {"type": "tool-call-delta", "name": name},
                                },
                            },
                        },
                        seq=seq,
                    )
            # Tool-call argument fragments are intentionally kept out of the
            # visible row; the integration already avoids surfacing secrets.
            _ = args
