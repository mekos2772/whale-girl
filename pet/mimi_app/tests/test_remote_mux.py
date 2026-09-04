"""Remote mux reader tests: stream lifecycle plus an opt-in live gate.

The deterministic half drives the real ``DshEventThread._run()`` loop over a
scripted in-process socket, so ``ready`` gating, ``session/control`` and
``session/follow`` decoding, sequence de-duplication and reconnect behaviour are
all covered without a server.

The live half (``RemoteMuxLiveGateTests``) talks to a real DSH on localhost. It
is opt-in, read-only by default and never prints a cookie, an auth URL, a client
id or session content. Run this file directly to see why the gate is closed:

    python mimi_app/tests/test_remote_mux.py
"""

from __future__ import annotations

import json
import os
import queue
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mimi_app" / "src"))

from mimi_pet.dsh_bridge import REMOTE_PATH, DshBridge, DshError, DshEventThread  # noqa: E402

LIVE_ENV = "MIMI_DSH_LIVE_MUX"
LIVE_SESSION_ENV = "MIMI_DSH_LIVE_MUX_SESSION"
TRUTHY = {"1", "true", "yes", "on"}


# --------------------------------------------------------------- scripted mux


class FakeTimeout(Exception):
    """Stands in for ``websocket.WebSocketTimeoutException``."""


class ScriptedSocket:
    """One scripted Remote-mux connection.

    Script entries are read in order by ``recv()``:

    * ``dict``   -- delivered to the reader as a JSON frame;
    * ``"idle"`` -- raise a timeout, which is how the reader picks up commands;
    * ``"close"`` -- return an empty payload, i.e. the peer hung up;
    * ``"stop"`` -- ask the reader to shut down cleanly;
    * callable   -- run it (e.g. ``set_sessions``) and keep reading.
    """

    def __init__(self, reader: DshEventThread, script: list) -> None:
        self._reader = reader
        self._script = list(script)
        self.sent: list[dict] = []
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, value) -> None:
        self.timeout = value

    def send(self, raw) -> None:
        self.sent.append(json.loads(raw))

    def recv(self):
        while True:
            if not self._script:
                self._reader._stop.set()
                raise FakeTimeout()
            step = self._script.pop(0)
            if callable(step):
                step()
                continue
            if step == "idle":
                raise FakeTimeout()
            if step == "close":
                return ""
            if step == "stop":
                self._reader._stop.set()
                raise FakeTimeout()
            return json.dumps(step, ensure_ascii=False)

    def close(self) -> None:
        self.closed = True

    # -------------------------------------------------------------- helpers

    def opened(self, stream_id: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == "open" and f.get("streamId") == stream_id]

    def cancelled(self, stream_id: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == "cancel" and f.get("streamId") == stream_id]


def item(stream_id: str, value) -> dict:
    return {"type": "item", "streamId": stream_id, "value": value}


def run_scripted(reader: DshEventThread, *scripts: list) -> list[ScriptedSocket]:
    """Run ``reader._run()`` against one scripted socket per connection."""
    sockets: list[ScriptedSocket] = []
    pending = [list(script) for script in scripts]

    def create_connection(url, **kwargs):
        sockets.append(ScriptedSocket(reader, pending.pop(0) if pending else ["stop"]))
        sockets[-1].url = url
        sockets[-1].kwargs = kwargs
        return sockets[-1]

    module = types.SimpleNamespace(
        create_connection=create_connection,
        WebSocketTimeoutException=FakeTimeout,
    )
    with mock.patch.dict(sys.modules, {"websocket": module}):
        reader._run()
    return sockets


def drain(events: "queue.Queue") -> list:
    return [events.get_nowait() for _ in range(events.qsize())]


def snapshot_frame() -> dict:
    """A follow snapshot holding one event row (seq 1) and one chunk row.

    The chunk row carries two texts, so it consumes sequences 2 and 3.
    """
    return {
        "type": "snapshot",
        "cursor": 3,
        "records": [
            {"type": "event", "event": {"type": "turn/start", "seq": 1, "time": 1, "data": {}}},
            {
                "type": "chunks",
                "event": {
                    "type": "chunkrow/text-chunks",
                    "seq": 2,
                    "time": 2,
                    "data": {"texts": ["你", "好"], "turn": 1, "step": 1},
                },
            },
        ],
        "hasMore": False,
    }


def make_reader(events: "queue.Queue", transitions: list[str]) -> DshEventThread:
    """A reader pinned to an unreachable host and never to ambient auth env."""
    return DshEventThread(
        host="127.0.0.1:9",
        events=events,
        on_connect=lambda: transitions.append("up"),
        on_disconnect=lambda: transitions.append("down"),
        auth_url="",
    )


# ----------------------------------------------------------- deterministic


class RemoteMuxStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.transitions: list[str] = []
        self.reader = make_reader(self.events, self.transitions)

    def test_ready_not_the_handshake_gates_the_link(self) -> None:
        seen: list[list[str]] = []
        sockets = run_scripted(
            self.reader,
            [
                lambda: seen.append(list(self.transitions)),
                # A ready without a clientId proves nothing and must not link.
                item("events", {"type": "ready"}),
                lambda: seen.append(list(self.transitions)),
                item("events", {"type": "ready", "clientId": "client-1"}),
                lambda: seen.append(list(self.transitions)),
                item("events", {"type": "emit", "event": "api-session/status", "args": ["s-1", True]}),
                "stop",
            ],
        )
        # Connected only after a ready that carries a client id.
        self.assertEqual(seen, [[], [], ["up"]])
        self.assertEqual(self.transitions, ["up", "down"])
        self.assertEqual(self.reader._client_id, "client-1")
        self.assertTrue(sockets[0].closed)

        socket = sockets[0]
        self.assertEqual(socket.url, f"ws://127.0.0.1:9{REMOTE_PATH}")
        self.assertNotIn("cookie", socket.kwargs)
        self.assertEqual(socket.opened("events")[0]["endpoint"], "$events")
        self.assertEqual(socket.opened("control")[0]["endpoint"], "session/control")

        decoded = drain(self.events)
        self.assertEqual([event.method for event in decoded], ["session/status"])
        self.assertEqual(decoded[0].payload, {"sessionId": "s-1", "running": True})

    def test_control_baseline_and_updates_decode_into_jobs(self) -> None:
        run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                item("control", {"type": "baseline", "value": {"jobs": {"s-1": [{"id": "j1", "status": "running"}]}}}),
                item("control", {"type": "jobs", "sessionId": "s-1", "jobs": [{"id": "j1", "status": "failed"}]}),
                "stop",
            ],
        )
        decoded = drain(self.events)
        self.assertEqual([event.method for event in decoded], ["session/jobs", "session/jobs"])
        self.assertEqual(decoded[0].payload["sessionId"], "s-1")
        self.assertEqual(decoded[0].payload["jobs"], [{"id": "j1", "status": "running"}])
        self.assertEqual(decoded[1].payload["jobs"], [{"id": "j1", "status": "failed"}])

    def test_follow_opens_for_desired_sessions_and_decodes_rows(self) -> None:
        self.reader.set_sessions(["s-1"])
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                item("follow:s-1", snapshot_frame()),
                "stop",
            ],
        )
        opened = sockets[0].opened("follow:s-1")
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["endpoint"], "session/follow")
        self.assertEqual(
            opened[0]["payload"]["args"]["request"],
            {"address": {"kind": "session", "sessionId": "s-1"}, "maxMessages": 100},
        )
        decoded = drain(self.events)
        self.assertEqual([event.method for event in decoded], ["session/event"] * 3)
        self.assertEqual([event.seq for event in decoded], [1, 2, 3])
        self.assertEqual(decoded[0].payload["event"]["type"], "turn/start")
        self.assertEqual(
            [event.payload["event"]["data"]["chunk"]["text"] for event in decoded[1:]],
            ["你", "好"],
        )

    def test_reconnect_replays_baseline_without_duplicating_sequences(self) -> None:
        self.reader.set_sessions(["s-1"])
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                item("follow:s-1", snapshot_frame()),
                "close",
            ],
            [
                item("events", {"type": "ready", "clientId": "client-2"}),
                # The server replays its baseline on resubscribe; seq 1..3 were
                # already delivered and must not reach the UI a second time.
                item("follow:s-1", snapshot_frame()),
                item("follow:s-1", {"type": "event", "event": {"type": "turn/end", "seq": 4, "time": 4, "data": {}}}),
                "stop",
            ],
        )
        self.assertEqual(len(sockets), 2)
        # Both connections open the core streams and resubscribe the session.
        for socket in sockets:
            self.assertEqual(len(socket.opened("events")), 1)
            self.assertEqual(len(socket.opened("control")), 1)
            self.assertEqual(len(socket.opened("follow:s-1")), 1)
        self.assertTrue(all(socket.closed for socket in sockets))
        self.assertEqual(self.transitions, ["up", "down", "up", "down"])

        decoded = drain(self.events)
        self.assertEqual([event.seq for event in decoded], [1, 2, 3, 4])
        self.assertEqual(decoded[-1].payload["event"]["type"], "turn/end")

    def test_events_stream_error_reconnects_while_follow_error_is_survivable(self) -> None:
        self.reader.set_sessions(["s-1"])
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                {"type": "error", "streamId": "events", "error": {"message": "events stream failed"}},
            ],
            [
                item("events", {"type": "ready", "clientId": "client-2"}),
                {"type": "error", "streamId": "follow:s-1", "error": {"message": "follow stream failed"}},
                # Still alive on the same connection after a per-stream error.
                item("control", {"type": "jobs", "sessionId": "s-1", "jobs": []}),
                "stop",
            ],
        )
        self.assertEqual(len(sockets), 2)
        self.assertEqual([event.method for event in drain(self.events)], ["session/jobs"])

    def test_stream_end_reopens_control_and_follow_on_the_same_connection(self) -> None:
        self.reader.set_sessions(["s-1"])
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                {"type": "end", "streamId": "control"},
                {"type": "end", "streamId": "follow:s-1"},
                "stop",
            ],
        )
        self.assertEqual(len(sockets), 1, "a per-stream end must not drop the mux")
        self.assertEqual(len(sockets[0].opened("control")), 2)
        self.assertEqual(len(sockets[0].opened("follow:s-1")), 2)

    def test_events_stream_end_forces_a_reconnect(self) -> None:
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                {"type": "end", "streamId": "events"},
            ],
            [
                item("events", {"type": "ready", "clientId": "client-2"}),
                "stop",
            ],
        )
        self.assertEqual(len(sockets), 2)

    def test_set_sessions_mid_stream_swaps_follow_subscriptions(self) -> None:
        self.reader.set_sessions(["s-1"])
        sockets = run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                lambda: self.reader.set_sessions(["s-2"]),
                # Commands are applied between reads, so let one read time out.
                "idle",
                item("follow:s-2", {"type": "event", "event": {"type": "turn/start", "seq": 1, "time": 1, "data": {}}}),
                "stop",
            ],
        )
        socket = sockets[0]
        self.assertEqual(len(socket.cancelled("follow:s-1")), 1)
        self.assertEqual(len(socket.opened("follow:s-2")), 1)
        decoded = drain(self.events)
        self.assertEqual([event.payload["sessionId"] for event in decoded], ["s-2"])

    def test_sequences_are_tracked_per_session(self) -> None:
        self.reader.set_sessions(["s-1", "s-2"])
        run_scripted(
            self.reader,
            [
                item("events", {"type": "ready", "clientId": "client-1"}),
                item("follow:s-1", {"type": "event", "event": {"type": "turn/start", "seq": 7, "time": 1, "data": {}}}),
                item("follow:s-2", {"type": "event", "event": {"type": "turn/start", "seq": 7, "time": 1, "data": {}}}),
                item("follow:s-1", {"type": "event", "event": {"type": "turn/start", "seq": 7, "time": 1, "data": {}}}),
                "stop",
            ],
        )
        decoded = drain(self.events)
        self.assertEqual([event.payload["sessionId"] for event in decoded], ["s-1", "s-2"])


# ------------------------------------------------------------------ live gate


def live_gate_status() -> tuple[bool, str]:
    """Whether the live mux gate may run, plus a reason that is safe to print."""
    if os.environ.get(LIVE_ENV, "").strip().lower() not in TRUTHY:
        return False, f"live mux gate is opt-in: set {LIVE_ENV}=1 to enable it"
    try:
        import websocket  # noqa: F401
    except Exception:
        return False, "websocket-client is not installed"
    host = os.environ.get("MIMI_DSH_HOST", "127.0.0.1:3080")
    try:
        DshBridge(host=host, timeout=2.0).rpc("session/list", {"_request": {}})
    except DshError as exc:
        # The message carries an HTTP status at most; no cookie or URL.
        if "HTTP 401" in str(exc):
            return False, "DSH answered 401: the plugin-supplied auth URL (MIMI_DSH_AUTH_URL) is required"
        return False, f"DSH is not usable: {exc}"
    except Exception as exc:
        return False, f"DSH is not reachable: {type(exc).__name__}"
    return True, f"DSH remote mux reachable at {host}"


LIVE_OK, LIVE_REASON = live_gate_status()


@unittest.skipUnless(LIVE_OK, LIVE_REASON)
class RemoteMuxLiveGateTests(unittest.TestCase):
    """Read-only checks against a real DSH remote mux.

    Nothing here reads another session's content: the gate only proves the
    handshake the decoders depend on. Following a stream needs a session of our
    own, so that part is a second opt-in that creates and archives one empty
    throwaway session.
    """

    def _reader(self) -> tuple[DshEventThread, "queue.Queue", list[str]]:
        events: queue.Queue = queue.Queue()
        transitions: list[str] = []
        reader = DshEventThread(
            host=os.environ.get("MIMI_DSH_HOST", "127.0.0.1:3080"),
            events=events,
            on_connect=lambda: transitions.append("up"),
            on_disconnect=lambda: transitions.append("down"),
        )
        return reader, events, transitions

    @staticmethod
    def _wait(predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_ready_arrives_and_the_link_stays_up(self) -> None:
        reader, _events, transitions = self._reader()
        reader.start()
        try:
            self.assertTrue(
                self._wait(lambda: transitions[:1] == ["up"], 15.0),
                "no ready frame with a client id within 15s",
            )
            # Never print the client id: only its presence is asserted.
            self.assertTrue(reader._client_id)
            time.sleep(2.0)
            self.assertEqual(transitions, ["up"], "the mux dropped after ready")
        finally:
            reader.stop()

    @unittest.skipUnless(
        os.environ.get(LIVE_SESSION_ENV, "").strip().lower() in TRUTHY,
        f"follow probe is opt-in ({LIVE_SESSION_ENV}=1): it creates and archives a throwaway session",
    )
    def test_follow_subscription_on_a_throwaway_session(self) -> None:
        bridge = DshBridge(host=os.environ.get("MIMI_DSH_HOST", "127.0.0.1:3080"))
        session_id = bridge.create_session()
        reader, events, transitions = self._reader()
        reader.start()
        try:
            self.assertTrue(self._wait(lambda: transitions[:1] == ["up"], 15.0), "no ready frame")
            reader.set_sessions([session_id])
            time.sleep(3.0)
            # An empty session has nothing to replay; what matters is that the
            # follow subscription neither errors out nor drops the mux.
            self.assertEqual(transitions, ["up"], "follow subscription dropped the mux")
            errors = [e for e in drain(events) if e.method == "session/error"]
            self.assertEqual(errors, [])
        finally:
            reader.stop()
            try:
                bridge.archive_session(session_id)
            except DshError as exc:
                self.fail(f"throwaway session {session_id!r} could not be archived: {exc}")


if __name__ == "__main__":
    # stderr keeps the verdict next to unittest's own report.
    print(f"remote mux live gate: {'OPEN' if LIVE_OK else 'CLOSED'} -- {LIVE_REASON}", file=sys.stderr)
    unittest.main(verbosity=2)
