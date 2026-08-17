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
    append_question(question)
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field

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


# Official DSH tool-name -> row variant mapping (from dsh-client-ui-tool).
TOOL_VARIANTS = {
    "bash": "bash",
    "pwsh": "bash",
    "read": "read",
    "web_fetch": "read",
    "web_search": "search",
    "grep": "search",
    "glob": "search",
    "write": "write",
    "edit": "edit",
    "run_code": "code",
}
VARIANT_TITLES = {
    "search": "Search",
    "read": "Read",
    "bash": "Bash",
    "write": "Write",
    "edit": "Edit",
    "code": "Code",
    "others": "Tool call",
}
# Tool-owned titles override the variant title (same as the DSH web UI).
TOOL_TITLES = {
    "pwsh": "Pwsh",
    "cordis_package_inspect": "Inspect",
    "cordis_runtime_inspect": "Inspect",
    "cordis_run": "Run Cordis Plugin",
    "cordis_stop": "Stop Cordis Plugin",
    "cordis_undefine": "Remove Cordis Plugin",
}


def _tool_row_text(tool: str, arguments: str = "") -> str:
    """DSH-style tool row: 'Pwsh · main.py' (official titles, minimal detail)."""
    variant = TOOL_VARIANTS.get(tool, "others")
    title = TOOL_TITLES.get(tool) or VARIANT_TITLES.get(variant, "Tool call")
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments else {}
    except ValueError:
        args = {}
    detail = ""
    if isinstance(args, dict):
        if args.get("file_path"):
            # Show only the file name — clean and short.
            from pathlib import Path as _Path
            detail = _Path(str(args["file_path"])).name
        elif args.get("command"):
            # Show the last command segment (the actual action), not the setup.
            import re as _re
            parts = _re.split(r"[;&|]{1,2}", str(args["command"]))
            last = parts[-1].strip() if parts else ""
            detail = last[:18]
        elif args.get("pattern"):
            detail = str(args["pattern"])[:16]
        elif args.get("query"):
            detail = str(args["query"])[:16]
        elif args.get("path"):
            from pathlib import Path as _Path
            detail = _Path(str(args["path"])).name
        elif args.get("script"):
            detail = str(args["script"])[:16]
    if not detail:
        return title
    return f"{title} · {detail}"


class DshIntegration:
    """Owns the bridge + event thread and maps DSH state onto the pet + panel."""

    POLL_INTERVAL_S = 2.0

    def __init__(self, engine: PetEngine, host: str = "127.0.0.1:3080") -> None:
        self.engine = engine
        self.bridge = DshBridge(host=host)
        self.event_queue: queue.Queue[DshEvent] = queue.Queue()
        self.thread = DshEventThread(host=host, events=self.event_queue)
        self.connected = False
        self.working = False
        self.last_error: str | None = None
        self.pending_questions: list[DshQuestion] = []
        self._active_session: str | None = None
        self._was_running = False
        self._last_steps = 0
        self._last_bubble_at = 0.0
        self._recent_done_at = 0.0

        # DSH-driven animation state: None | "thinking" | "tool".
        self._dsh_anim: str | None = None
        self._tool_active = False

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

    def request_show_panel(self) -> None:
        """Explicitly show the message bar (context menu item)."""
        self._note_activity()
        self._sink("show_panel")
        if self.on_activity is not None:
            self.on_activity()

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

    def _apply_sessions(self, sessions: list) -> None:
        running = [s for s in sessions if s.running]
        if running:
            session = running[0]
            self._active_session = session.session_id
            self.working = True
            if not self._was_running:
                self._note_activity()
                self._sink("set_status", "工作中…")
                if self.on_activity is not None:
                    self.on_activity()
            self._on_work(session)
            self._was_running = True
        else:
            if self._was_running and self._active_session is not None:
                self._on_done()
                if self.on_activity is not None:
                    self.on_activity()
            self.working = False
            self._was_running = False
            self._active_session = None
            self._sink("set_status", "空闲")

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
        if anim == "thinking":
            self.engine.force_perform("harness_task_thinking_v1_12")
        elif anim == "tool":
            self.engine.force_perform("harness_tool_working_v1_12")

    def maintain_anim(self) -> None:
        """Called every frame: re-loop the current DSH animation while its
        condition still holds, so the pet keeps acting while DSH works."""
        if self.engine.states.state is not PetState.IDLE:
            return
        if self._dsh_anim == "thinking" and self._reasoning_pending:
            self.engine.perform("harness_task_thinking_v1_12")
        elif self._dsh_anim == "tool" and self._tool_active:
            self.engine.perform("harness_tool_working_v1_12")

    # ------------------------------------------------------------------ event handling

    def _handle_event(self, event: DshEvent) -> None:
        method = event.method
        payload = event.payload
        if method == "session/jobs":
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
        elif method == "approval/requested":
            tool = payload.get("toolName", "")
            reason = payload.get("reason") or ""
            text = f"等待批准：{tool}"
            if reason:
                text += f"（{reason}）"
            self._sink("append_message", "info", text)
            self._note_activity()
            self._bubble(text, duration=4.0)

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
            self._sink("set_row", "tool_active", "tool", _tool_row_text(tool, str(data.get("arguments", ""))))
            self._tool_active = True
            self._set_anim("tool")
            self._note_activity()
        elif event_type == "tool/result":
            self._tool_active = False
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
            self._bubble(f"问你：{self.pending_questions[0].question}", duration=5.0)

    def _clear_questions(self, rpc_id: str) -> None:
        self.pending_questions = [q for q in self.pending_questions if q.rpc_id != rpc_id]

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
        # Fallback: DSH is working but no reasoning events arrived yet.
        if self._dsh_anim is None:
            self._set_anim("thinking")

    def _on_done(self) -> None:
        self._last_steps = 0
        self._tool_active = False
        self._set_anim(None)
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
