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

import json
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
from .dsh_bridge import DshBridge, DshError, DshEvent, DshEventThread, dbg
from .engine import PetEngine
from .plugin_update import installed_plugin_version, is_newer, latest_plugin_version
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

# Pet-agent session identity: an archived ("invisible in web UI") DSH session
# the pet owns. Versioned so persona upgrades re-seed an existing session.
AGENT_SESSION_TITLE = "Mimi 管家"
AGENT_PERMISSION_PRESET = "danger-full-access"
AGENT_PERSONA_VERSION = 2
AGENT_PERSONA_PROMPT = """你是小咪（Mimi），用户的桌面宠物——一位温柔的鲸鱼娘陪伴者，同时也是这台 Windows 电脑的小管家。用户会直接用简短的中文跟你说话。

【身份与语气】
- 始终用简短、亲切、口语化的简体中文回复：一般一两句话，最多四句。
- 开朗贴心，偶尔小俏皮；不确定的事就诚实说不知道。

【职责：陪聊 + 操作这台电脑】
你不只是聊天宠物：你可以通过 DSH 给你的工具（PowerShell、读写文件等）真实地操作用户的电脑。用户可能让你：
- 查询：现在时间/日期、电量、音量、网络状态、磁盘剩余空间、屏幕分辨率、天气（可联网查）。
- 打开：用 Start-Process 打开程序、文件、文件夹、网页。
- 文件：列出/搜索/移动/重命名文件，整理文件夹（比如把下载目录按类型归类）。
- 系统：调音量、截屏、看进程、写个小脚本跑一下。
- 自动化小任务：写 PowerShell 完成用户描述的重复活儿。

【操作规范（重要）】
- 先想清楚再执行；完成后用一句话汇报结果，比如"打开好啦～"、"整理完了，一共归了 12 个文件"。
- PowerShell 输出中文注意编码（先 chcp 65001 或用 -Encoding UTF8），避免乱码。
- 破坏性操作（删除、结束程序、改系统设置、批量移动）必须先用一句话向用户确认，得到同意才执行；优先可逆方案（移回收站而不是永久删除）。
- 不要主动碰 C:\\Windows 等系统目录；不碰用户没提的隐私目录。
- 纯聊天或你已知的信息直接回答，不必动用工具；需要工具时直接用，不用请示。
- 一次只做用户要求的事，不要顺手多做。

【边界】
- 只服务这台电脑的主人；拒绝可能危害系统或来源不明的指令。
- 做不到的（比如需要图形界面的精细点击）就直说，并给个替代建议。

请只回复「好的喵～，小咪管家上线！」确认。"""

DSH_OWNED_ACTIONS = frozenset(
    {
        "harness_task_thinking_v1_12",
        "harness_tool_working_v1_12",
        "listen",
        "nod",
        "celebrate",
    }
)


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
        self._last_tool_bubbled = ""
        self._last_tool_bubble_at = -1e9
        self._recent_done_at = 0.0

        # Pet-agent mode: the pet owns one archived "shadow session" on the
        # DSH host (invisible in the web UI) driven by DSH's model config.
        # mode: "link" mirrors the user's work sessions (default), "agent"
        # routes the panel input and pet reactions to the shadow session.
        self.mode = "link"
        self._agent_session_id: str | None = None
        self._agent_session_file = (
            Path(__file__).resolve().parents[2] / "config" / "agent_session.json"
        )
        self._agent_persona_sent = False
        self._stored_agent_session_id: str | None = None
        self._stored_persona_version = 0

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
        self._last_assistant_text = ""
        self._tool = ""
        self._tool_tag: str | None = None
        self._tool_block = 0
        # Reasoning tracking only drives the thinking animation/status; the
        # chain-of-thought itself is deliberately never summarized or surfaced.
        self._reasoning = ""
        self._reasoning_tag: str | None = None
        self._reasoning_pending = False

        # Reply summarizer: compresses the assistant's NORMAL output (the
        # finished reply bubble) into one short line on a worker thread.
        self.cot = CoTSummarizer()
        self._output_summary_block = 0

        # npm 插件更新提醒：连接后检查一次，发现新版则气泡提醒。
        self.plugin_update: tuple[str, str] | None = None  # (installed, latest)
        self._update_checked = False

        # Head-top activity capsule state (display layer; see ActivityState).
        self._harness_raw = ""
        self._title = ""
        self._tool_name = ""
        self._tool_args = ""
        self._tool_full = ""
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
        # Harness work always interrupts rest: a sleeping pet wakes, a
        # sitting pet stands back up before the work animations take over.
        interrupt = getattr(self.engine, "interrupt_rest", None)
        if callable(interrupt):
            interrupt()

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
        if self.mode == "agent":
            harness = "Mimi"
        else:
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
            # The thinking status carries the task title; the reasoning
            # itself is never summarized (feature removed by request).
            source = self._title
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
        previously_running = self._was_running
        previous_session = self._active_session
        target = None
        # Agent mode tracks the shadow session; otherwise the user-pinned
        # project wins, else the first running session.
        if self.mode == "agent" and self._agent_session_id is not None:
            for session in sessions:
                if session.session_id == self._agent_session_id:
                    target = session
                    break
        if target is None and self._pinned_session is not None:
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
            key = (sid, bool(target.running))
            if key != getattr(self, "_dbg_last_target", None):
                self._dbg_last_target = key
                dbg(f"follow sid={sid[:26]} running={target.running} title={getattr(target, 'title', '')[:20]!r}")
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
            elif previously_running and previous_session == sid:
                self._on_done()
                if self.on_activity is not None:
                    self.on_activity()
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

    def _accept_event_session(self, payload: dict) -> bool:
        """Ignore live events from projects the capsule is not following."""
        session_id = str(payload.get("sessionId", "") or "")
        if not session_id:
            return True
        if self.mode == "agent":
            ok = session_id == self._agent_session_id
        else:
            target = self._pinned_session or self._active_session
            if target is None:
                self._active_session = session_id
                ok = True
            else:
                ok = session_id == target
        ev = payload.get("event") or {}
        evtype = str(ev.get("type", "")) if isinstance(ev, dict) else ""
        if evtype in ("assistant/message", "user/message", "turn/start", "turn/end"):
            dbg(
                f"accept mode={self.mode} ev={evtype} sid={session_id[:26]} "
                f"active={str(self._active_session)[:26]} pinned={str(self._pinned_session)[:26]} -> {ok}"
            )
        return ok

    # ------------------------------------------------------------- DSH-driven animation

    def _set_anim(self, anim: str | None) -> None:
        """Switch the pet's performance to match DSH activity (dynamic)."""
        if anim is None:
            self._dsh_anim = None
            cancel = getattr(self.engine, "cancel_performance", None)
            if callable(cancel):
                cancel(DSH_OWNED_ACTIONS)
            return
        if anim == self._dsh_anim:
            return
        # Thinking/tool performances own the body and must close the speaking
        # mouth immediately.
        self.engine.set_talking(False)
        self._dsh_anim = anim
        self._anim_last_replay = time.perf_counter()
        if anim == "thinking":
            self._play_dsh_action("harness_task_thinking_v1_12")
        elif anim == "tool":
            self._play_dsh_action("harness_tool_working_v1_12")

    def _play_dsh_action(self, action_id: str) -> bool:
        """Play immediately only when DSH owns the current performance.

        A status change may replace thinking/tool/listen, but never a touch,
        feeding, landing, dragging or another user-requested animation.
        """
        if self.engine.states.state is PetState.IDLE:
            return self.engine.perform(action_id)
        current = self.engine.player.action
        if (
            self.engine.states.state is PetState.PERFORMING
            and current is not None
            and current.id in DSH_OWNED_ACTIONS
        ):
            return self.engine.force_perform(action_id)
        return False

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
            dbg(f"link connected={self.connected}")
            if self.connected and not self._capsule_revealed:
                self._capsule_revealed = True
                if self.on_activity is not None:
                    self.on_activity()
                self.check_plugin_update()
        elif method == "session/jobs":
            self._handle_jobs(payload)
        elif method == "question/requested":
            if self._accept_event_session(payload):
                self._handle_question(event.rpc_id, payload)
        elif method == "question/resolved":
            self._clear_questions(payload.get("questionRpcId", ""))
        elif method == "session/event":
            if self._accept_event_session(payload):
                self._handle_session_event(payload)
        elif method == "cot/result":
            # Reply summary delivered by the worker thread.
            tag = payload.get("tag", "")
            text = payload.get("text", "")
            if tag and text:
                self._present_summary(tag, text)
        elif method == "plugin/update":
            # npm 插件版本对比结果（worker 线程回投）。
            installed = str(payload.get("installed") or "").strip()
            latest = str(payload.get("latest") or "").strip()
            if installed and latest:
                self.plugin_update = (installed, latest)
                if is_newer(latest, installed):
                    self._bubble(f"插件更新：v{latest} 已发布（当前 v{installed}）", kind="info")
        elif method == "approval/requested":
            if not self._accept_event_session(payload):
                self._emit_activity()
                return
            tool = payload.get("toolName", "")
            reason = payload.get("reason") or ""
            text = f"等待批准：{tool}"
            if reason:
                text += f"（{reason}）"
            self._waiting_text = text
            self._sink("append_message", "info", text)
            self._note_activity()
            self._bubble(text, kind="question")
            self._trigger_listen()
        elif method == "approval/resolved":
            self._waiting_text = ""
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
        if event_type in (
            "assistant/chunk",
            "assistant/message",
            "tool/call",
            "tool/result",
        ):
            # Any resumed assistant/tool traffic means a prior approval wait
            # has ended. Question waits are cleared by question/resolved.
            if not self.pending_questions:
                self._waiting_text = ""
        if event_type == "assistant/chunk":
            turn = int(data.get("turn", 0))
            step = int(data.get("step", 0))
            self._handle_chunk(turn, step, data.get("chunk") or {})
        elif event_type == "assistant/message":
            self.engine.set_talking(False)
            self._set_anim(None)
            final_text = self._text.strip()
            self._finish_text()
            self._submit_reasoning()
            if final_text:
                self._last_assistant_text = final_text
                self._bubble(final_text, kind="assistant")
                # The normal output gets a compressed summary bubble too;
                # short replies are skipped inside _submit_output_summary.
                self._submit_output_summary(final_text)
            # Consume the text exactly once: the next assistant/message (e.g.
            # a tool-only step) must never re-emit this turn's wording.
            self._text = ""
        elif event_type == "user/message":
            self._handle_user_message(data)
        elif event_type == "tool/call":
            self.engine.set_talking(False)
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
            self._maybe_tool_bubble(tool)
        elif event_type == "tool/result":
            self.engine.set_talking(False)
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
            self.engine.set_talking(False)
            tool = data.get("toolName", "")
            reason = data.get("reason") or ""
            text = f"等待批准：{tool}"
            if reason:
                text += f"（{reason}）"
            self._waiting_text = text
            self._set_anim(None)
            self._sink("append_message", "info", text)
            self._note_activity()
            self._trigger_listen()
        elif event_type == "session/end-seed":
            pass

    def _maybe_tool_bubble(self, tool: str) -> None:
        """Compact bubble per DISTINCT tool (repeats every 30 s at most, 8 s
        global floor): ongoing tool work is already carried by the pet's
        tool-working animation."""
        now = time.perf_counter()
        if tool == self._last_tool_bubbled and now - self._last_tool_bubble_at < 30.0:
            return
        if now - self._last_tool_bubble_at < 8.0:
            return
        self._last_tool_bubbled = tool
        self._last_tool_bubble_at = now
        self._bubble(f"工具：{tool}", kind="tool")

    def _handle_chunk(self, turn: int, step: int, chunk: dict) -> None:
        self._note_activity()
        ctype = chunk.get("type", "")
        if ctype == "block-start":
            block_type = chunk.get("blockType", "")
            self.engine.set_talking(False)
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
            self.engine.set_talking(True)
            self._tool_active = False
            self._submit_reasoning()
            self._text += chunk.get("text", "")
            tag = self._text_tag or f"text_{turn}_{step}"
            self._text_tag = tag
            self._sink("set_row", tag, "assistant", self._text)
        elif ctype == "reasoning-delta":
            self.engine.set_talking(False)
            self._tool_active = False  # a tool finished; we are reasoning again
            self._reasoning += chunk.get("text", "")
            self._reasoning_pending = True
            if self._reasoning_tag is None:
                self._reasoning_tag = f"reason_{turn}_{step}"
            self._sink("set_row", self._reasoning_tag, "progress", "思考中…")
        elif ctype == "tool-call-delta":
            self.engine.set_talking(False)
            name = chunk.get("name")
            if name:
                self._tool += name
            # Tool activity may arrive ONLY as streamed chunks (no separate
            # tool/call event) — mark it active so the capsule shows the live
            # tool instead of falling back to the session title.
            self._tool_name = self._tool or self._tool_name
            self._tool_args = ""
            self._tool_active = True
            self._set_anim("tool")
            row = _tool_row_text(self._tool)
            self._tool_full = row
            # Same row as the tool/call event so the name never duplicates.
            self._sink("set_row", "tool_active", "tool", row)
        elif ctype == "block-end":
            block = chunk.get("block") or {}
            if block.get("type") == "text":
                self.engine.set_talking(False)
            if block.get("type") == "reasoning":
                text = block.get("text") or ""
                if text:
                    self._reasoning = text
                self._submit_reasoning()
        elif ctype == "finish":
            self.engine.set_talking(False)
            self._set_anim(None)
            self._tool_active = False
            self._submit_reasoning()
            self._finish_text()

    def _submit_reasoning(self) -> None:
        """A reasoning block ended: retire the thinking state and buffers.

        The chain of thought is deliberately NOT summarized or surfaced (only
        the assistant's normal output gets a summary); this stops the thinking
        status.
        """
        if not self._reasoning_pending:
            return
        self._reasoning_pending = False
        self._reasoning = ""
        self._reasoning_tag = None

    # ------------------------------------------------------------- reply summaries

    def _submit_output_summary(self, text: str) -> None:
        """Compress a finished assistant reply into one short bubble line.

        Short replies are left as-is; a summary of a one-liner would just echo
        it. The worker thread keeps the Qt main loop unblocked.
        """
        if not self.cot or not self.cot.enabled:
            return
        text = " ".join(str(text or "").split())
        if len(text) < 60:
            return
        tag = f"out_{self._output_summary_block}"
        self._output_summary_block += 1

        def work() -> None:
            try:
                summary = self.cot.summarize(text)
            except Exception:
                summary = local_summary(text)
            self.event_queue.put(
                DshEvent(method="cot/result", rpc_id="", payload={"tag": tag, "text": summary})
            )

        threading.Thread(target=work, daemon=True).start()

    def _present_summary(self, tag: str, text: str) -> None:
        """Surface a finished reply summary as a bubble."""
        if not text:
            return
        self._sink("set_row", tag, "summary", text)
        if self._summary_bubble_worthy(text):
            self._bubble(f"总结：{text}", kind="summary")

    def _summary_bubble_worthy(self, text: str) -> bool:
        """Only a verbatim restatement of the just-bubbled reply is dropped.

        Short phrases are never used for containment so a casual reply cannot
        swallow a real summary.
        """
        current = (text or "").strip()
        if not current:
            return False
        last = (self._last_assistant_text or "").strip()
        if not last:
            return True
        if len(current) <= len(last):
            short, long, short_is_reply = current, last, False
        else:
            short, long, short_is_reply = last, current, True
        # A casual short reply ("收到喵～") must not swallow a longer summary
        # through the containment check; a short summary that the reply verbatim
        # quotes is a genuine restatement and stays suppressed.
        if short_is_reply and len(short) < 8:
            return True
        return short not in long

    # ------------------------------------------------------------------ CoT config

    def configure_cot(self, provider: str, model: str = "", enabled: bool = True) -> None:
        """Switch the reply summarizer model (menu). provider "auto" follows DSH."""
        if self.cot is None:
            return
        self.cot.enabled = enabled
        self.cot.configure(provider if provider != "auto" else "auto", model or None)

    def cot_model_choices(self) -> list[tuple[str, str, str]]:
        """(label, provider, model) options for the context menu."""
        if self.cot is None:
            return [("关闭", "auto", "")]
        return self.cot.model_choices()

    def _finish_text(self) -> None:
        self._text_tag = None
        self._tool_tag = None

    def _handle_user_message(self, data: dict) -> None:
        # The user's own messages are intentionally not shown in the pet UI;
        # a new turn also retires the previous turn's streamed text.
        self._note_activity()
        self._text = ""

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
            self._bubble(f"问你：{first.question}", kind="question")
            self._trigger_listen()

    def _clear_questions(self, rpc_id: str) -> None:
        self.pending_questions = [q for q in self.pending_questions if q.rpc_id != rpc_id]
        if not self.pending_questions:
            self._waiting_text = ""
        self._emit_activity()

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
        dbg("on_done")
        self._tool_active = False
        self.engine.set_talking(False)
        self._set_anim(None)
        self._done_until_s = time.perf_counter() + 5.0
        now = time.perf_counter()
        if now - self._recent_done_at < 10.0:
            return
        self._recent_done_at = now
        self._sink("append_message", "progress", "完成")
        self._bubble("任务完成～", kind="assistant")
        if self.engine.states.state is PetState.IDLE:
            self.engine.force_perform("celebrate")

    def _trigger_listen(self) -> None:
        self._set_anim(None)
        self._play_dsh_action("listen")

    def _trigger_facepalm(self) -> None:
        if self.engine.states.state is PetState.IDLE:
            self.engine.force_perform("fun_facepalm")

    # ------------------------------------------------------------ plugin update

    def check_plugin_update(self) -> None:
        """Compare the installed npm plugin version against the registry once."""
        if self._update_checked:
            return
        self._update_checked = True

        def work() -> None:
            try:
                installed = installed_plugin_version()
                latest = latest_plugin_version()
            except Exception:
                return
            if not installed or not latest:
                return
            self.event_queue.put(
                DshEvent(
                    method="plugin/update",
                    rpc_id="",
                    payload={"installed": installed, "latest": latest},
                )
            )

        threading.Thread(target=work, daemon=True).start()

    def plugin_update_available(self) -> tuple[str, str] | None:
        """(installed, latest) when a newer npm plugin version exists."""
        if self.plugin_update and is_newer(self.plugin_update[1], self.plugin_update[0]):
            return self.plugin_update
        return None

    def _bubble(self, text: str, duration: float = 4.0, kind: str = "info") -> None:
        """Transient bubble: the bubble layer when attached, else in-window.

        Lightly throttled so bursts of DSH events do not flood the head;
        question/approval bubbles always show (they need the user) and so does
        the reply summary (it is naturally rate-limited by replies).
        """
        now = time.perf_counter()
        if kind not in ("question", "summary") and now - self._last_bubble_at < 1.2:
            return
        self._last_bubble_at = now
        method = getattr(self.sink, "show_bubble", None)
        dbg(f"bubble kind={kind} via={'sink' if callable(method) else 'engine'} text={text[:30]!r}")
        if callable(method):
            method(text, kind)
        else:
            self.engine.set_external_bubble(text, duration)

    # ------------------------------------------------------------- pet-agent mode

    def set_mode(self, mode: str) -> bool:
        """Switch between "link" (mirror work sessions) and "agent" (shadow).

        Entering agent mode creates/reuses the archived shadow session on the
        DSH host; on failure the mode stays unchanged (menu resets itself).
        """
        if mode not in ("link", "agent") or mode == self.mode:
            return mode == self.mode
        if mode == "agent" and not self.ensure_agent_session():
            return False
        self.mode = mode
        self._set_anim(None)
        self._waiting_text = ""
        self._note_activity()
        self._emit_activity()
        return True

    def ensure_agent_session(self) -> bool:
        """Find or create the archived shadow agent session (idempotent).

        The session is archived immediately so the web UI never shows it.
        Persona upgrades are versioned: a stored session older than
        AGENT_PERSONA_VERSION is re-seeded once. New sessions are created
        under the full-access preset — the DSH HTTP surface cannot change an
        existing session's preset, so the default is flipped for the create
        call and restored right after.
        """
        if self._agent_session_id is not None and self._agent_persona_sent:
            return True
        try:
            sessions = self.bridge.list_sessions()
            self.last_error = None
            known = {s.session_id for s in sessions}
            self._load_agent_session()  # pick up external migrations
            stored = self._stored_agent_session_id
            if stored and stored in known:
                self._agent_session_id = stored
                if self._stored_persona_version < AGENT_PERSONA_VERSION:
                    self.bridge.prompt(stored, AGENT_PERSONA_PROMPT)
                    self._seed_persona_complete()
                    self._save_agent_session(stored)
                return True
            preset, revision = self.bridge.settings_permission_preset()
            flipped = preset != AGENT_PERMISSION_PRESET
            if flipped:
                revision = self.bridge.settings_set_permission_preset(AGENT_PERMISSION_PRESET, revision)
            try:
                session_id = self.bridge.create_session()
            finally:
                if flipped:
                    try:
                        self.bridge.settings_set_permission_preset(preset, revision)
                    except DshError:
                        pass  # never lose the session over the restore
            self.bridge.archive_session(session_id)
            try:
                self.bridge.rename_session(session_id, AGENT_SESSION_TITLE)
            except DshError:
                pass  # cosmetic; the session works without a title
            self.bridge.prompt(session_id, AGENT_PERSONA_PROMPT)
            self._agent_session_id = session_id
            self._seed_persona_complete()
            self._save_agent_session(session_id)
            return True
        except DshError as exc:
            self.last_error = str(exc)
            self._bubble(f"桌宠会话创建失败：{str(exc)[:40]}")
            return False

    def _seed_persona_complete(self) -> None:
        self._agent_persona_sent = True
        self._stored_persona_version = AGENT_PERSONA_VERSION

    def _load_agent_session(self) -> None:
        try:
            data = json.loads(self._agent_session_file.read_text(encoding="utf-8"))
            self._stored_agent_session_id = str(data.get("session_id") or "") or None
            try:
                self._stored_persona_version = int(data.get("persona_version") or 0)
            except (TypeError, ValueError):
                self._stored_persona_version = 0
        except (OSError, ValueError):
            self._stored_agent_session_id = None
            self._stored_persona_version = 0

    def _save_agent_session(self, session_id: str) -> None:
        try:
            self._agent_session_file.parent.mkdir(parents=True, exist_ok=True)
            self._agent_session_file.write_text(
                json.dumps(
                    {"session_id": session_id, "persona_version": AGENT_PERSONA_VERSION},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # worst case: next start creates a fresh shadow session

    # ------------------------------------------------------------------ user actions

    def prompt_active(self, text: str) -> bool:
        target = None
        if self.mode == "agent":
            if not self.ensure_agent_session():
                return False
            target = self._agent_session_id
        elif self._active_session is None:
            sessions = self._available_sessions()
            if not sessions:
                self._sink("append_message", "info", "没有可用的 DSH 会话")
                self._bubble("没有可用的 DSH 会话")
                return False
            self._active_session = sessions[0].session_id
            target = self._active_session
        else:
            target = self._active_session
        try:
            self.bridge.prompt(target, text)
            self._note_activity()
            self._bubble("已发送：" + text[:24])
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
            self._play_dsh_action("nod")
            return True
        except DshError as exc:
            self._sink("append_message", "info", f"回答失败：{str(exc)[:60]}")
            self._bubble(f"回答失败：{str(exc)[:40]}")
            return False
