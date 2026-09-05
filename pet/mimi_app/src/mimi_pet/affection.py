"""Persistent relationship state for Mimi's desktop-pet interactions.

The tracker deliberately stays independent from DSH sessions and from the
engine's transient expression state.  It can therefore be used by the plain
engine tests without importing Qt, while the Qt application may persist it
through :class:`AffectionStore`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .debug_log import dbg

SCHEMA_VERSION = 1
DEFAULT_SCORE = 50
MIN_SCORE = 0
MAX_SCORE = 100
MAX_DAILY_GAIN = 12
CHAT_SOURCE = "chat_positive"
CHAT_REWARD = 1
CHAT_COOLDOWN_S = 90.0
CHAT_MAX_DAILY_GAIN = 3
CHAT_FINGERPRINT_TTL_S = 24 * 60 * 60
CHAT_MAX_FINGERPRINTS = 256

# The values are intentionally small: affection should describe a relationship
# over time, not reward a single burst of clicks.
DEFAULT_REWARDS: dict[str, int] = {
    "touch_head": 1,
    "touch_face": 1,
    "touch_belly": 1,
    "touch_hand": 1,
    "touch_combo": 2,
    "feed_bread": 2,
    "double_click": 2,
    "welcome_back": 1,
    "file_drop": 1,
    "agent_reply": 1,
    "agent_answer": 1,
    CHAT_SOURCE: CHAT_REWARD,
}

# Cooldowns are per source and persist across restarts.  They stop a rapid
# click loop from filling the relationship meter while allowing different
# kinds of interaction to feel distinct.
DEFAULT_COOLDOWNS_S: dict[str, float] = {
    "touch_head": 2.0,
    "touch_face": 2.0,
    "touch_belly": 2.0,
    "touch_hand": 2.0,
    "touch_combo": 5.0,
    "feed_bread": 8.0,
    "double_click": 5.0,
    "welcome_back": 90.0,
    "file_drop": 5.0,
    "agent_reply": 12.0,
    "agent_answer": 5.0,
    CHAT_SOURCE: CHAT_COOLDOWN_S,
}

SOURCE_LABELS: dict[str, str] = {
    "touch_head": "摸头",
    "touch_face": "戳脸",
    "touch_belly": "挠肚子",
    "touch_hand": "击掌",
    "touch_combo": "摸头后击掌",
    "feed_bread": "投喂面包",
    "double_click": "双击互动",
    "welcome_back": "欢迎回来",
    "file_drop": "接收文件",
    "agent_reply": "Mimi 回复",
    "agent_answer": "回答确认",
    CHAT_SOURCE: "暖心聊天",
}


@dataclass(frozen=True)
class ChatAffectionClassification:
    """Pure classification result for one user-authored chat message."""

    eligible: bool
    category: str
    normalized_text: str
    fingerprint: str
    reason: str = ""


_CHAT_POSITIVE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("greeting", ("你好", "您好", "嗨", "早安", "早上好", "午安", "晚安", "晚上好")),
    ("gratitude", ("谢谢", "感谢", "辛苦了", "麻烦你了")),
    ("care", ("你还好吗", "还好吗", "累不累", "要休息吗", "吃饭了吗")),
    ("companionship", ("陪陪我", "陪我聊", "陪我吧", "抱抱", "给你抱抱", "在陪我")),
    ("praise", ("你真棒", "你好可爱", "好可爱", "好厉害", "喜欢你", "最喜欢你")),
    ("encouragement", ("加油", "辛苦啦", "做得好", "我支持你")),
    ("relationship", ("想你", "想你了", "我回来了", "回来啦", "欢迎回来", "晚点见", "明天见")),
)

_CHAT_WORK_MARKERS = (
    "帮我",
    "写代码",
    "运行",
    "执行",
    "删除",
    "修改",
    "打开",
    "关闭",
    "安装",
    "卸载",
    "项目",
    "文件",
    "脚本",
    "终端",
    "命令",
    "代码",
    "程序",
    "设置",
    "截图",
    "搜索",
    "查询",
    "总结",
    "整理",
    "下载",
    "上传",
    "发送",
    "发布",
    "生成",
    "创建",
    "移动",
    "复制",
    "重命名",
    "重启",
    "关机",
    "powershell",
    "python",
    "npm",
    "git ",
)


def _normalize_chat_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return re.sub(r"\s*([，。！？、；：,.!?;:])\s*", r"\1", normalized)


_CHAT_REPEAT_EXCEPTIONS = frozenset({"抱抱", "谢谢"})


def _is_pure_repetition(compact: str) -> bool:
    if compact in _CHAT_REPEAT_EXCEPTIONS or len(compact) < 2:
        return False
    for unit_size in range(1, len(compact) // 2 + 1):
        if len(compact) % unit_size == 0 and len(compact) // unit_size >= 2:
            unit = compact[:unit_size]
            if unit * (len(compact) // unit_size) == compact:
                return True
    return False


def classify_chat_affection(text: str) -> ChatAffectionClassification:
    """Classify conservative, relationship-oriented user chat.

    This deliberately uses a small positive vocabulary instead of sentiment
    scoring.  A false positive would be an easy way to turn work commands or
    repeated filler into an affection farming loop.
    """
    normalized = _normalize_chat_text(text)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if not normalized:
        return ChatAffectionClassification(False, "empty", normalized, fingerprint, "empty")
    if normalized.startswith(("/", "\\")):
        return ChatAffectionClassification(False, "command", normalized, fingerprint, "command")
    if "```" in normalized or re.search(r"(?:[a-z]:\\|https?://|^\$\s|\b(?:cmd|bash|powershell|python|npm|git)\b)", normalized):
        return ChatAffectionClassification(False, "work", normalized, fingerprint, "work_content")

    compact = re.sub(r"[\s，。！？、；：,.!?;:~～]+", "", normalized)
    if len(compact) < 2:
        return ChatAffectionClassification(False, "too_short", normalized, fingerprint, "too_short")
    if _is_pure_repetition(compact):
        return ChatAffectionClassification(False, "repeated", normalized, fingerprint, "repeated")
    if any(marker in normalized for marker in _CHAT_WORK_MARKERS):
        return ChatAffectionClassification(False, "work", normalized, fingerprint, "work_content")

    for category, phrases in _CHAT_POSITIVE_PATTERNS:
        if any(phrase in compact for phrase in phrases):
            return ChatAffectionClassification(True, category, normalized, fingerprint)
    return ChatAffectionClassification(False, "neutral", normalized, fingerprint, "not_positive")


def source_label(source: str) -> str:
    """Return a short, user-safe label for a recorded interaction source."""
    return SOURCE_LABELS.get(source, source.replace("_", " ").strip() or "互动")


@dataclass(frozen=True)
class AffectionSnapshot:
    score: int
    level: str
    level_name: str
    last_interaction: str = ""
    last_interaction_at: float = 0.0
    daily_gain: int = 0


@dataclass(frozen=True)
class AffectionChange:
    source: str
    requested_delta: int
    applied_delta: int
    score_before: int
    score_after: int
    level_before: str
    level_after: str
    recorded: bool
    reason: str = ""

    @property
    def stage_changed(self) -> bool:
        return self.level_before != self.level_after


_LEVELS: tuple[tuple[int, str, str], ...] = (
    (0, "初识", "初识"),
    (25, "熟悉", "熟悉"),
    (60, "亲近", "亲近"),
    (85, "默契", "默契"),
)


def affection_level(score: int) -> tuple[str, str]:
    """Return the stable key and Chinese display name for ``score``."""
    clamped = max(MIN_SCORE, min(MAX_SCORE, int(score)))
    current = _LEVELS[0]
    for entry in _LEVELS:
        if clamped >= entry[0]:
            current = entry
        else:
            break
    return current[1], current[2]


class AffectionTracker:
    """Bounded, rate-limited relationship score.

    ``now`` is a monotonic-like numeric value for event cooldowns.  ``day`` is
    an ISO date string used for the daily cap and is injectable for deterministic
    tests.  The tracker itself has no filesystem or Qt dependency.
    """

    def __init__(
        self,
        score: int = DEFAULT_SCORE,
        *,
        daily_date: str | None = None,
        daily_gain: int = 0,
        last_interaction: str = "",
        last_interaction_at: float = 0.0,
        last_events: Mapping[str, float] | None = None,
        max_daily_gain: int = MAX_DAILY_GAIN,
        rewards: Mapping[str, int] | None = None,
        cooldowns_s: Mapping[str, float] | None = None,
        chat_daily_date: str | None = None,
        chat_daily_gain: int = 0,
        chat_last_at: float = 0.0,
        chat_fingerprints: Mapping[str, float] | None = None,
    ) -> None:
        self.score = self._clamp_score(score)
        self.daily_date = daily_date or _dt.date.today().isoformat()
        self.daily_gain = max(0, int(daily_gain))
        self.last_interaction = str(last_interaction or "")
        self.last_interaction_at = float(last_interaction_at or 0.0)
        self.last_events = {
            str(key): float(value)
            for key, value in (last_events or {}).items()
            if isinstance(key, str) and self._is_number(value)
        }
        self.max_daily_gain = max(0, int(max_daily_gain))
        self.rewards = {**DEFAULT_REWARDS, **dict(rewards or {})}
        self.cooldowns_s = {**DEFAULT_COOLDOWNS_S, **dict(cooldowns_s or {})}
        self.chat_daily_date = chat_daily_date or self.daily_date
        self.chat_daily_gain = max(0, int(chat_daily_gain))
        self.chat_last_at = float(chat_last_at or 0.0)
        self.chat_fingerprints = {
            str(key): float(value)
            for key, value in (chat_fingerprints or {}).items()
            if isinstance(key, str) and self._is_number(value)
        }

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _clamp_score(score: Any) -> int:
        if isinstance(score, bool):
            raise ValueError("affection score must be an integer")
        try:
            value = int(score)
        except (TypeError, ValueError) as exc:
            raise ValueError("affection score must be an integer") from exc
        return max(MIN_SCORE, min(MAX_SCORE, value))

    @property
    def level(self) -> str:
        return affection_level(self.score)[0]

    @property
    def level_name(self) -> str:
        return affection_level(self.score)[1]

    def snapshot(self) -> AffectionSnapshot:
        return AffectionSnapshot(
            score=self.score,
            level=self.level,
            level_name=self.level_name,
            last_interaction=self.last_interaction,
            last_interaction_at=self.last_interaction_at,
            daily_gain=self.daily_gain,
        )

    def record(
        self,
        source: str,
        delta: int | None = None,
        *,
        now: float | None = None,
        day: str | None = None,
    ) -> AffectionChange:
        """Record one successful interaction and return the accounting result."""
        source = str(source or "interaction")
        requested = int(self.rewards.get(source, 1) if delta is None else delta)
        before = self.score
        level_before = self.level
        now_value = float(time.time() if now is None else now)
        day_value = day or _dt.date.today().isoformat()

        if day_value != self.daily_date:
            self.daily_date = day_value
            self.daily_gain = 0

        cooldown = max(0.0, float(self.cooldowns_s.get(source, 0.0)))
        previous = self.last_events.get(source)
        if previous is not None and now_value - previous < cooldown:
            return AffectionChange(
                source,
                requested,
                0,
                before,
                before,
                level_before,
                level_before,
                False,
                "cooldown",
            )

        if requested == 0:
            return AffectionChange(
                source,
                requested,
                0,
                before,
                before,
                level_before,
                level_before,
                False,
                "zero",
            )

        if requested > 0:
            remaining = max(0, self.max_daily_gain - self.daily_gain)
            applied = min(requested, remaining, MAX_SCORE - before)
            reason = "daily_limit" if applied < requested else ""
        else:
            applied = max(requested, MIN_SCORE - before)
            reason = "score_floor" if applied > requested else ""

        if applied == 0:
            return AffectionChange(
                source,
                requested,
                0,
                before,
                before,
                level_before,
                level_before,
                False,
                reason or "score_limit",
            )

        self.score += applied
        if applied > 0:
            self.daily_gain += applied
        self.last_events[source] = now_value
        self.last_interaction = source
        self.last_interaction_at = now_value
        level_after = self.level
        return AffectionChange(
            source,
            requested,
            applied,
            before,
            self.score,
            level_before,
            level_after,
            True,
            reason,
        )

    def _prune_chat_state(self, *, now: float | None = None) -> None:
        if now is not None:
            self.chat_fingerprints = {
                fingerprint: timestamp
                for fingerprint, timestamp in self.chat_fingerprints.items()
                if now - timestamp <= CHAT_FINGERPRINT_TTL_S
            }
        if len(self.chat_fingerprints) > CHAT_MAX_FINGERPRINTS:
            recent = sorted(
                self.chat_fingerprints.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:CHAT_MAX_FINGERPRINTS]
            self.chat_fingerprints = dict(recent)

    @staticmethod
    def _not_recorded(
        source: str,
        requested: int,
        before: int,
        level: str,
        reason: str,
    ) -> AffectionChange:
        return AffectionChange(
            source,
            requested,
            0,
            before,
            before,
            level,
            level,
            False,
            reason,
        )

    def record_chat(
        self,
        text: str,
        delta: int | None = None,
        *,
        now: float | None = None,
        day: str | None = None,
    ) -> AffectionChange:
        """Record one eligible, user-authored desktop-pet chat message."""
        classification = classify_chat_affection(text)
        requested = int(self.rewards.get(CHAT_SOURCE, CHAT_REWARD) if delta is None else delta)
        before = self.score
        level_before = self.level
        if not classification.eligible:
            return self._not_recorded(
                CHAT_SOURCE,
                requested,
                before,
                level_before,
                classification.reason or "not_positive",
            )

        now_value = float(time.time() if now is None else now)
        day_value = day or _dt.date.today().isoformat()
        if day_value != self.chat_daily_date:
            self.chat_daily_date = day_value
            self.chat_daily_gain = 0
        self._prune_chat_state(now=now_value)

        if classification.fingerprint in self.chat_fingerprints:
            return self._not_recorded(
                CHAT_SOURCE,
                requested,
                before,
                level_before,
                "duplicate",
            )

        previous = self.chat_last_at
        if previous <= 0:
            previous = float(self.last_events.get(CHAT_SOURCE, 0.0) or 0.0)
        if previous and now_value - previous < CHAT_COOLDOWN_S:
            return self._not_recorded(
                CHAT_SOURCE,
                requested,
                before,
                level_before,
                "chat_cooldown",
            )

        remaining = max(0, CHAT_MAX_DAILY_GAIN - self.chat_daily_gain)
        if remaining <= 0:
            return self._not_recorded(
                CHAT_SOURCE,
                requested,
                before,
                level_before,
                "chat_daily_limit",
            )
        if requested <= 0:
            return self._not_recorded(
                CHAT_SOURCE,
                requested,
                before,
                level_before,
                "zero",
            )

        effective_requested = min(requested, remaining)
        change = self.record(CHAT_SOURCE, effective_requested, now=now_value, day=day_value)
        if not change.recorded:
            return change
        if effective_requested < requested:
            change = AffectionChange(
                CHAT_SOURCE,
                requested,
                change.applied_delta,
                change.score_before,
                change.score_after,
                change.level_before,
                change.level_after,
                change.recorded,
                "chat_daily_limit",
            )
        self.chat_daily_gain += change.applied_delta
        self.chat_last_at = now_value
        self.chat_fingerprints[classification.fingerprint] = now_value
        self._prune_chat_state()
        return change

    def to_payload(self) -> dict[str, Any]:
        self._prune_chat_state()
        return {
            "schema_version": SCHEMA_VERSION,
            "affection": self.score,
            "daily_date": self.daily_date,
            "daily_gain": self.daily_gain,
            "last_interaction": self.last_interaction,
            "last_interaction_at": self.last_interaction_at,
            "last_events": dict(self.last_events),
            "chat_state": {
                "daily_date": self.chat_daily_date,
                "daily_gain": self.chat_daily_gain,
                "last_at": self.chat_last_at,
                "fingerprints": dict(self.chat_fingerprints),
            },
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AffectionTracker":
        if not isinstance(payload, Mapping):
            raise ValueError("affection state must be an object")
        schema = payload.get("schema_version", SCHEMA_VERSION)
        if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_VERSION:
            raise ValueError("unsupported affection state schema")
        score = payload.get("affection", DEFAULT_SCORE)
        if isinstance(score, bool) or not isinstance(score, int):
            raise ValueError("affection must be an integer")
        daily_gain = payload.get("daily_gain", 0)
        if isinstance(daily_gain, bool) or not isinstance(daily_gain, int) or daily_gain < 0:
            raise ValueError("daily_gain must be a non-negative integer")
        daily_date = payload.get("daily_date") or _dt.date.today().isoformat()
        if not isinstance(daily_date, str):
            raise ValueError("daily_date must be text")
        last_interaction = payload.get("last_interaction", "")
        if not isinstance(last_interaction, str):
            raise ValueError("last_interaction must be text")
        last_at = payload.get("last_interaction_at", 0.0)
        if not cls._is_number(last_at):
            raise ValueError("last_interaction_at must be numeric")
        last_events = payload.get("last_events", {})
        if not isinstance(last_events, Mapping):
            raise ValueError("last_events must be an object")
        for key, value in last_events.items():
            if not isinstance(key, str) or not cls._is_number(value):
                raise ValueError("last_events contains invalid data")
        chat_state = payload.get("chat_state", {})
        if chat_state is None:
            chat_state = {}
        if not isinstance(chat_state, Mapping):
            raise ValueError("chat_state must be an object")
        chat_daily_date = chat_state.get("daily_date", daily_date)
        if not isinstance(chat_daily_date, str):
            raise ValueError("chat_state.daily_date must be text")
        chat_daily_gain = chat_state.get("daily_gain", 0)
        if (
            isinstance(chat_daily_gain, bool)
            or not isinstance(chat_daily_gain, int)
            or chat_daily_gain < 0
        ):
            raise ValueError("chat_state.daily_gain must be a non-negative integer")
        chat_last_at = chat_state.get("last_at", 0.0)
        if not cls._is_number(chat_last_at):
            raise ValueError("chat_state.last_at must be numeric")
        chat_fingerprints = chat_state.get("fingerprints", {})
        if not isinstance(chat_fingerprints, Mapping):
            raise ValueError("chat_state.fingerprints must be an object")
        for key, value in chat_fingerprints.items():
            if not isinstance(key, str) or not cls._is_number(value):
                raise ValueError("chat_state.fingerprints contains invalid data")
        return cls(
            score=score,
            daily_date=daily_date,
            daily_gain=daily_gain,
            last_interaction=last_interaction,
            last_interaction_at=float(last_at),
            last_events=last_events,
            chat_daily_date=chat_daily_date,
            chat_daily_gain=chat_daily_gain,
            chat_last_at=float(chat_last_at),
            chat_fingerprints=chat_fingerprints,
        )


class AffectionStore:
    """Load and atomically save the user's relationship state."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else self.default_path()

    @staticmethod
    def default_path() -> Path:
        override = os.environ.get("MIMI_STATE_PATH")
        if override:
            return Path(override).expanduser()
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "MimiDesktopPet" / "state.json"
        return Path.home() / ".mimi-desktop-pet" / "state.json"

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        counter = 1
        while backup.exists():
            backup = self.path.with_name(
                f"{self.path.stem}.corrupt-{stamp}-{counter}{self.path.suffix}"
            )
            counter += 1
        try:
            os.replace(self.path, backup)
            dbg(f"affection state quarantined: {backup}")
        except OSError as exc:
            dbg(f"affection state quarantine failed: {exc}")

    def load(self) -> AffectionTracker:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return AffectionTracker.from_payload(payload)
        except FileNotFoundError:
            return AffectionTracker()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            dbg(f"affection state reset: {exc}")
            self._quarantine()
            return AffectionTracker()

    def save(self, tracker: AffectionTracker) -> bool:
        payload = tracker.to_payload()
        parent = self.path.parent
        temporary: str | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            dbg(f"affection state save failed: {exc}")
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


__all__ = [
    "AffectionChange",
    "AffectionSnapshot",
    "AffectionStore",
    "AffectionTracker",
    "DEFAULT_REWARDS",
    "MAX_DAILY_GAIN",
    "affection_level",
    "source_label",
]
