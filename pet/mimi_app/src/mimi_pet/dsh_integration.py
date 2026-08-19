"""Qt-side DSH integration: status polling, live event streaming and pet reactions.

The DshBridge/DshEventThread are pure Python; this module adapts them to the
pet: a top message bar (DshPanel) mirrors the DSH web UI — live assistant
text, tool calls, turn/step progress, user questions and approvals — while
the pet keeps light bubble reactions. The panel is optional: any object
implementing the sink methods below can be attached via ``sink``.

Sink protocol (all optional, duck-typed):
    show_panel()
    append_message(role, text)
    set_row(tag, role, text)
    upsert_progress(text)
    set_status(text)
    set_activity(state)   # ActivityState for the head-top capsule
    append_question(question)
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .activity_text import (
    getCompactActivityText,
    getHarnessDisplayName,
    tool_row_text,
    tool_summary,
)
from .cot_summarizer import CoTSummarizer, local_summary
from .dsh_bridge import DshBridge, DshError, DshEvent, DshEventThread
from .engine import PetEngine
from .state_machine import PetState


@dataclass
class DshQuestion:
    rpc_id: str
    session_id: str
    id: str
    question: str
    detail: str = ""
    header: str = ""
    options: list[dict] = field(default_factory=list)
    multi_select: bool = False


@dataclass(frozen=True)
class ActivityState:
    """What the head-top capsule shows — display-layer only, never sent back.

    Field meanings
    --------------
    harness_display_name  short badge ("DSH"/"Codex"/"Claude"), "" -> hidden
    status                stable key: connected/thinking/executing/tool/
                          waiting/done/fail/disconnected
    status_zh             human label for the tooltip ("思考中")
    summary               the compressed 动作 + 对象 line for the capsule
    full_activity         full un-shortened text for the hover tooltip
    is_connected          whether the DSH event/tcp link is alive
    is_waiting_for_user   a pending question/approval (stronger click affordance)
    """

    harness_display_name: str
    status: str
    status_zh: str
    summary: str
    full_activity: str
    is_connected: bool = False
    is_waiting_for_user: bool = False


# Keep the old private name working where it was referenced externally.
_tool_row_text = tool_row_text  # noqa: N816 (legacy alias)


class DshIntegration:
    """Owns the bridge + event thread and maps DSH state onto the pet + panel."""

    POLL_INTERVAL_S = 2.0
    REPLAY_GAP_S = 10.0  # min seconds between DSH work-animation replays

    def __init__(self, engine: PetEngine, host: str = "127.0.0.1:3080") -> None:
        self.engine = engine
        self.bridge = DshBridge(host=host)
        self.event_queue: queue.Queue[DshEvent] = queue.Queue()
        self.connected = False
        self._capsule_revealed = False
        # IMPORTANT: the event-thread callbacks run on the websocket thread.
        # They must never touch Qt/widgets directly (that deadlocks the GUI
        # main loop) — they only post a marker event that the main-thread
        # drain (QTimer) turns into a UI update.
        self.thread = DshEventThread(
            host=host,
            events=self.event_queue,
            on_connect=lambda: self._queue_link(True),
            on_disconnect=lambda: self._queue_link(False),
        )
        self.working = False
        self.last_error: str | None = None
        self.pending_questions: list[DshQuestion] = []
        self._active_session: str | None = None
        self._was_running = False
        self._last_steps = 0
        self._last_bubble_at = 0.0
        self._recent_done_at = 0.0

        # DSH-driven animation state: None | "thinking" | "tool".
        # Replays are throttled so the pet breathes/sways between work cycles
        # instead of staying frozen inside a looping work animation forever.
        self._dsh_anim: str | None = None
        self._tool_active = False
        self._anim_last_replay = 0.0

        # Async polling: HTTP happens on a worker thread so the Qt main
        # thread never blocks (a stalled DSH request must not freeze the pet).
        self._poll_queue: queue.Queue = queue.Queue()
        self._polling = False

        # Panel sink (DshPanel or anything duck-typed); callbacks are optional.
        self.sink = None
        self.on_activity = None  # callable(active: bool) -> panel show/hide
        self.last_activity_at = 0.0

        # Streaming state for assistant/chunk deltas.
        self._text = ""
        self._text_tag: str | None = None
        self._text_block = 0
        self._tool = ""
        self._tool_tag: str | None = None
        self._tool_block = 0
        self._reasoning = ""
        self._reasoning_tag: str | None = None
        self._reasoning_pending = False

        # Chain-of-thought summarizer (worker thread delivers via event queue).
        self.cot = CoTSummarizer()

        # Head-top activity capsule state (display layer; see ActivityState).
        self._harness_raw = ""
        self._title = ""
        self._tool_name = ""
        self._tool_args = ""
        self._tool_full = ""
        self._summary_text = ""  # latest CoT summary (compressed for the capsule)
        self._waiting_text = ""
        self._done_until_s = 0.0
        # Multi-project support: DSH can advance several sessions at once.
        # _pinned_session lets the user choose WHICH project the capsule
        # tracks (and which session messages go to); None = follow the first
        # running session automatically.
        self._known_sessions: list = []
        self._pinned_session: str | None = None
        self.activity = ActivityState(
            harness_display_name="",
            status="disconnected",
            status_zh="未连接",
            summary="未连接…",
            full_activity="",
        )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.thread.stop()

    def _available_sessions(self) -> list:
        try:
            sessions = self.bridge.list_sessions()
            self.last_error = None
            return sessions
        except DshError as exc:
            self.last_error = str(exc)
            return []

    # ------------------------------------------------------------------ sink helpers

    def _sink(self, name: str, *args) -> None:
        sink = self.sink
        if sink is None:
            return
        method = getattr(sink, name, None)
        if callable(method):
            method(*args)

    def _note_activity(self) -> None:
        self.last_activity_at = time.time()

    def _queue_link(self, connected: bool) -> None:
        """Called from the websocket thread: hand off link state to the main
        thread via the shared event queue (drained by a QTimer)."""
        self.event_queue.put(
            DshEvent(method="__link", rpc_id="", payload={"connected": connected})
        )

    # ------------------------------------------------------------ activity capsule

    def _pick_tool_state(self) -> tuple[str, str]:
        """(tool_capsule_summary, tool_full_row) for the running tool, if any."""
        if not self._tool_active:
            return "", ""
        row = tool_row_text(self._tool_name, self._tool_args)
        return tool_summary(self._tool_name, self._tool_args), row

    def _build_activity(self) -> ActivityState:
        connected = bool(self.connected)
        harness = getHarnessDisplayName(self._harness_raw) or ("DSH" if connected else "")
        waiting = bool(self._waiting_text)
        tool = self._tool_active

        if self.last_error and not connected:
            summary, full = "连接失败…", f"无法连接 DSH：{self.last_error[:80]}"
            return ActivityState(harness, "fail", "连接失败", summary, full, False, False)
        if not connected and not self._active_session:
            summary, full = "未连接…", "尚未连接 DeepSeek Harness"
            return ActivityState(harness, "disconnected", "已断开", summary, full, False, False)
        if waiting:
            text = self._waiting_text
            return ActivityState(
                harness, "waiting", "等待输入",
                getCompactActivityText(text) or "等待你确认…", text, True, True,
            )
        if tool:
            summary, full = self._pick_tool_state()
            return ActivityState(harness, "tool", "调用工具", summary, full, True, False)
        if self._reasoning_pending:
            # Current task summary takes priority while reasoning; fall back.
            source = self._summary_text or self._title
            if source:
                return ActivityState(
                    harness, "thinking", "思考中",
                    getCompactActivityText(source), source, True, False,
                )
            return ActivityState(harness, "thinking", "思考中", "思考中…", "思考中…", True, False)
        if self.working:
            source = self._title
            if source:
                return ActivityState(
                    harness, "executing", "执行中",
                    getCompactActivityText(source), source, True, False,
                )
            return ActivityState(harness, "executing", "执行中", "执行中…", "DSH 会话运行中", True, False)
        # Connected and idle: show the last-known session goal if we remember one.
        now = time.perf_counter()
        if now < self._done_until_s:
            return ActivityState(harness, "done", "已完成", "已完成…", "任务完成", True, False)
        title = self._title
        if title:
            return ActivityState(
                harness, "connected", "已连接",
                getCompactActivityText(title), title, True, False,
            )
        return ActivityState(harness, "connected", "已连接", "DSH 已连接", "已连接 DeepSeek Harness", True, False)

    def _emit_activity(self) -> None:
        """Push the current ActivityState to the panel sink (content only)."""
        self.activity = self._build_activity()
        self._sink("set_activity", self.activity)

    def request_show_panel(self) -> None:
        """Explicitly show the message bar (context menu item).

        Order matters: on_activity first shows the head-top capsule, then
        show_panel expands it to the full list (panel stays the final state).
        """
        self._note_activity()
        if self.on_activity is not None:
            self.on_activity()
        self._sink("show_panel")

    # ------------------------------------------------------------------ polling (QTimer)

    def poll_status(self) -> None:
        """Sync poll (tests / fallback): call on a worker thread in production."""
        self._apply_sessions(self._available_sessions())

    def poll_status_async(self) -> None:
        """Worker-thread poll: never blocks the Qt main thread."""
        if self._polling:
            return
        self._polling = True

        def work() -> None:
            try:
                sessions = self.bridge.list_sessions()
                self._poll_queue.put(("ok", sessions))
            except DshError as exc:
                self._poll_queue.put(("err", str(exc)))
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def drain_poll(self) -> None:
        """Apply finished poll results on the main thread (QTimer)."""
        while True:
            try:
                kind, value = self._poll_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "ok":
                self.last_error = None
                self._apply_sessions(value)
            else:
                self.last_error = str(value)
                self._emit_activity()

    def _apply_sessions(self, sessions: list) -> None:
        self._known_sessions = list(sessions)
        target = None
        # User-pinned project wins, otherwise the first running session.
        if self._pinned_session is not None:
            for session in sessions:
                if session.session_id == self._pinned_session:
                    target = session
                    break
        if target is None:
            for session in sessions:
                if session.running:
                    target = session
                    break
        if target is not None:
            sid = target.session_id
            was_active = self._active_session == sid
            self._active_session = sid
            self._harness_raw = getattr(target, "agent_preset", "") or ""
            if getattr(target, "title", ""):
                self._title = target.title
            running = bool(target.running)
            if running:
                if not was_active:
                    self._note_activity()
                    self._sink("set_status", "工作中…")
                    if self.on_activity is not None:
                        self.on_activity()
                self._on_work(target)
            self.working = running
            self._was_running = running
        else:
            if self._was_running and self._active_session is not None:
                self._on_done()
                if self.on_activity is not None:
                    self.on_activity()
            self.working = False
            self._was_running = False
            self._active_session = None
            self._sink("set_status", "空闲")
        self._emit_activity()

    # ------------------------------------------------------------ project picker

    def session_choices(self) -> list[dict]:
        """(session_id, label, running) for every known DSH session."""
        out = []
        for s in self._known_sessions:
            name = Path(s.cwd).name if s.cwd else s.session_id[-8:]
            title = (s.title or "").strip()
            label = f"{name} · {title[:16]}" if title else name
            out.append(
                {
                    "session_id": s.session_id,
                    "label": label,
                    "running": bool(s.running),
                    "title": title[:16],
                }
            )
        return out

    def pinned_session(self) -> str | None:
        return self._pinned_session

    def select_session(self, session_id: str) -> None:
        """Pin the project the capsule tracks; "" (empty) = auto-follow."""
        self._pinned_session = session_id or None
        # Re-evaluate against the last-known session list immediately.
        self._apply_sessions(list(self._known_sessions))
        if self.on_activity is not None:
            self.on_activity()

    def drain_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_event(event)

    # ------------------------------------------------------------- DSH-driven animation

    def _set_anim(self, anim: str | None) -> None:
        """Switch the pet's performance to match DSH activity (dynamic)."""
        if anim == self._dsh_anim:
            return
        self._dsh_anim = anim
        self._anim_last_replay = time.perf_counter()
        if anim == "thinking":
            self.engine.force_perform("harness_task_thinking_v1_12")
        elif anim == "tool":
            self.engine.force_perform("harness_tool_working_v1_12")

    def maintain_anim(self) -> None:
        """Re-loop the current DSH work animation, but throttled.

        Replaying on every idle frame would freeze the pet inside the moving
        action forever; a fresh replay only happens after a gap so the pet
        returns to its natural standing sway between work cycles.
        """
        if self.engine.states.state is not PetState.IDLE:
            return
        now = time.perf_counter()
        if now - self._anim_last_replay < self.REPLAY_GAP_S:
            return
        if self._dsh_anim == "thinking" and self._reasoning_pending:
            self._anim_last_replay = now
            self.engine.perform("harness_task_thinking_v1_12")
        elif self._dsh_anim == "tool" and self._tool_active:
            self._anim_last_replay = now
            self.engine.perform("harness_tool_working_v1_12")

    # ------------------------------------------------------------------ event handling

    def _handle_event(self, event: DshEvent) -> None:
        method = event.method
        payload = event.payload
        if method == "__link":
            # Processed on the main thread (drained by QTimer) — safe to touch
            # Qt here. First successful link reveals the head-top capsule.
            self.connected = bool(payload.get("connected"))
            if self.connected and not self._capsule_revealed:
                self._capsule_revealed = True
                if self.on_activity is not None:
                    self.on_activity()
        elif method == "session/jobs":
            self._handle_jobs(payload)
        elif method == "question/requested":
            self._handle_question(event.rpc_id, payload)
        elif method == "question/resolved":
            self._clear_questions(payload.get("questionRpcId", ""))
        elif method == "session/event":
            self._handle_session_event(payload)
        elif method == "cot/result":
            # Summarized reasoning delivered by the worker thread.
            tag = payload.get("tag", "")
            text = payload.get("text", "")
            if tag and text:
                self._sink("set_row", tag, "summary", text)
                self._summary_text = text
        elif method == "approval/requested":
            tool = payload.get("toolName", "")
            reason = payload.get("reason") or ""
            text = f"等待批准：{tool}"
            if reason:
                text += f"（{reason}）"
            self._waiting_text = text
            self._sink("append_message", "info", text)
            self._note_activity()
            self._bubble(text, duration=4.0)
        self._emit_activity()

    def _handle_jobs(self, payload: dict) -> None:
        # Running jobs are already surfaced as tool rows (工具：pwsh), so we
        # do not duplicate them here; only failures get a message.
        if any(job.get("status") == "failed" for job in payload.get("jobs", [])):
            self._sink("append_message", "info", "有任务失败")
            self._trigger_facepalm()

    # ------------------------------------------------------------- session events

    def _handle_session_event(self, payload: dict) -> None:
        event = payload.get("event") or {}
        event_type = event.get("type", "")
        data = event.get("data") or {}
        if event_type == "assistant/chunk":
            turn = int(data.get("turn", 0))
            step = int(data.get("step", 0))
            self._handle_chunk(turn, step, data.get("chunk") or {})
        elif event_type == "assistant/message":
            self._set_anim(None)
            self._finish_text()
            self._submit_reasoning()
        elif event_type == "user/message":
            self._handle_user_message(data)
        elif event_type == "tool/call":
            self._finish_text()
            self._submit_reasoning()
            tool = data.get("name", "")
            self._tool_name = tool
            self._tool_args = str(data.get("arguments", ""))
            row = _tool_row_text(tool, self._tool_args)
            self._tool_full = row
            self._sink("set_row", "tool_active", "tool", row)
            self._tool_active = True
            self._set_anim("tool")
            self._note_activity()
        elif event_type == "tool/result":
            self._tool_active = False
            self._tool_name = ""
            self._tool_args = ""
            self._tool_full = ""
            self._set_anim(None)
            if data.get("error"):
                error = data["error"]
                self._sink(
                    "set_row", "tool_active", "info",
                    f"出错：{error.get('name', '')} ({error.get('code', '')})",
                )
            else:
                self._sink("set_row", "tool_active", "progress", "完成")
            self._note_activity()
        elif event_type in ("step/start", "turn/start"):
            turn = data.get("turn", 0)
            step = data.get("step", 0)
            self._sink("set_status", f"第 {turn} 轮 · 第 {step} 步")
            self._note_activity()
        elif event_type == "approval/asked":
            tool = data.get("toolName", "")
            reason = data.get("reason") or ""
            text = f"等待批准：{tool}"
            if reason:
                text += f"（{reason}）"
            self._waiting_text = text
            self._set_anim(None)
            self._sink("append_message", "info", text)
            self._note_activity()
        elif event_type == "session/end-seed":
            pass

    def _handle_chunk(self, turn: int, step: int, chunk: dict) -> None:
        self._note_activity()
        ctype = chunk.get("type", "")
        if ctype == "block-start":
            block_type = chunk.get("blockType", "")
            if block_type == "text":
                self._submit_reasoning()
                self._finish_text()
                self._text = ""
                self._text_block += 1
                self._text_tag = f"text_{self._text_block}"
            elif block_type == "tool-call":
                self._submit_reasoning()
                self._finish_text()
                self._tool = ""
                self._tool_block += 1
                self._tool_tag = f"tool_{self._tool_block}"
            elif block_type == "reasoning":
                self._reasoning = ""
                self._reasoning_pending = True
                self._summary_text = ""  # previous block's summary is stale now
                self._reasoning_tag = f"reason_{turn}_{step}"
                self._sink("set_row", self._reasoning_tag, "progress", "思考中…")
                self._set_anim("thinking")
        elif ctype == "text-delta":
            self._set_anim(None)  # assistant is talking -> back to idle
            self._submit_reasoning()
            self._text += chunk.get("text", "")
            tag = self._text_tag or f"text_{turn}_{step}"
            self._text_tag = tag
            self._sink("set_row", tag, "assistant", self._text)
        elif ctype == "reasoning-delta":
            self._reasoning += chunk.get("text", "")
            self._reasoning_pending = True
            if self._reasoning_tag is None:
                self._reasoning_tag = f"reason_{turn}_{step}"
            self._sink("set_row", self._reasoning_tag, "progress", "思考中…")
        elif ctype == "tool-call-delta":
            name = chunk.get("name")
            if name:
                self._tool += name
            # Same row as the tool/call event so the name never duplicates.
            self._sink("set_row", "tool_active", "tool", _tool_row_text(self._tool))
        elif ctype == "block-end":
            block = chunk.get("block") or {}
            if block.get("type") == "reasoning":
                text = block.get("text") or ""
                if text:
                    self._reasoning = text
                self._submit_reasoning()
        elif ctype == "finish":
            self._set_anim(None)
            self._submit_reasoning()
            self._finish_text()

    def _submit_reasoning(self) -> None:
        """Send collected reasoning to the summarizer (worker thread)."""
        if not self._reasoning_pending:
            return
        self._reasoning_pending = False
        text = self._reasoning
        tag = self._reasoning_tag
        self._reasoning = ""
        self._reasoning_tag = None
        if not text.strip() or not tag:
            return
        if not self.cot or not self.cot.enabled or len(text.strip()) < 80:
            self._sink("set_row", tag, "summary", local_summary(text))
            return
        self._sink("set_row", tag, "progress", "思考中…")

        def work() -> None:
            try:
                summary = self.cot.summarize(text)
            except Exception:
                summary = local_summary(text)
            self.event_queue.put(
                DshEvent(method="cot/result", rpc_id="", payload={"tag": tag, "text": summary})
            )

        threading.Thread(target=work, daemon=True).start()

    def _finish_text(self) -> None:
        self._text_tag = None
        self._tool_tag = None

    def _handle_user_message(self, data: dict) -> None:
        # The user's own messages are intentionally not shown in the pet UI.
        self._note_activity()

    # --------------------------------------------------------------- questions

    def _handle_question(self, rpc_id: str, payload: dict) -> None:
        session_id = payload.get("sessionId", "")
        for raw in payload.get("questions", []):
            question = DshQuestion(
                rpc_id=rpc_id,
                session_id=session_id,
                id=str(raw.get("id", "")),
                question=str(raw.get("question", "")),
                detail=str(raw.get("detail", "") or ""),
                header=str(raw.get("header", "") or ""),
                options=list(raw.get("options") or []),
                multi_select=bool(raw.get("multiSelect", False)),
            )
            self.pending_questions.append(question)
            self._sink("append_question", question)
            self._note_activity()
        if self.pending_questions:
            first = self.pending_questions[0]
            self._waiting_text = first.header or first.question or "等待你确认…"
            self._bubble(f"问你：{first.question}", duration=5.0)

    def _clear_questions(self, rpc_id: str) -> None:
        self.pending_questions = [q for q in self.pending_questions if q.rpc_id != rpc_id]
        if not self.pending_questions:
            self._waiting_text = ""
        self._emit_activity()

    # ------------------------------------------------------------------ CoT config

    def configure_cot(self, provider: str, model: str = "", enabled: bool = True) -> None:
        """Switch the reasoning summarizer model (menu). provider "auto" follows DSH."""
        if self.cot is None:
            return
        self.cot.enabled = enabled
        self.cot.configure(provider if provider != "auto" else "auto", model or None)

    def cot_model_choices(self) -> list[tuple[str, str, str]]:
        """(label, provider, model) options for the context menu."""
        if self.cot is None:
            return [("关闭", "auto", "")]
        return self.cot.model_choices()

    # ------------------------------------------------------------------ pet reactions

    def _on_work(self, session) -> None:
        steps = session.steps
        if steps != self._last_steps:
            self._last_steps = steps
            self._sink("set_status", f"工作中 · 第 {steps} 步")
        # No forced animation here: a merely-"running" session with no live
        # reasoning/tool events must not spin the pet. The thinking loop is
        # started by real reasoning blocks (see _handle_chunk) and replayed
        # with a gap in maintain_anim.
        self._note_activity()

    def _on_done(self) -> None:
        self._last_steps = 0
        self._tool_active = False
        self._set_anim(None)
        self._done_until_s = time.perf_counter() + 5.0
        now = time.perf_counter()
        if now - self._recent_done_at < 10.0:
            return
        self._recent_done_at = now
        self._sink("append_message", "progress", "完成")
        if self.engine.states.state is PetState.IDLE:
            self.engine.force_perform("fun_proud_smug")

    def _trigger_facepalm(self) -> None:
        if self.engine.states.state is PetState.IDLE:
            self.engine.force_perform("fun_facepalm")

    def _bubble(self, text: str, duration: float = 4.0) -> None:
        self.engine.set_external_bubble(text, duration)

    # ------------------------------------------------------------------ user actions

    def prompt_active(self, text: str) -> bool:
        if self._active_session is None:
            sessions = self._available_sessions()
            if not sessions:
                self._sink("append_message", "info", "没有可用的 DSH 会话")
                self._bubble("没有可用的 DSH 会话")
                return False
            self._active_session = sessions[0].session_id
        try:
            self.bridge.prompt(self._active_session, text)
            self._note_activity()
            self._bubble(f"已发送：{text[:24]}")
            return True
        except DshError as exc:
            self._sink("append_message", "info", f"发送失败：{str(exc)[:60]}")
            self._bubble(f"发送失败：{str(exc)[:40]}")
            return False

    def answer_question(self, question: DshQuestion, selected: list[str], custom: str = "") -> bool:
        try:
            answer = {"id": question.id, "selected": selected}
            if custom:
                answer["custom"] = custom
            self.bridge.respond(question.rpc_id, question.session_id, [answer])
            self._clear_questions(question.rpc_id)
            text = "/".join(selected) or custom or "(空)"
            self._bubble(f"已回答：{text}")
            return True
        except DshError as exc:
            self._sink("append_message", "info", f"回答失败：{str(exc)[:60]}")
            self._bubble(f"回答失败：{str(exc)[:40]}")
            return False
