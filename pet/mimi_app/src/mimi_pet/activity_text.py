"""Presentation layer for the head-top harness activity capsule.

Pure Python (no Qt): turns raw harness activity — session title, active tool,
CoT/reasoning summary, status and pending questions — into the short
"动作 + 对象" strings the capsule displays, plus the harness display name.
The Harness backend protocol is never touched: this module only reshapes
already-available data for the UI.

Key functions
-------------
getHarnessDisplayName(raw)
    Map a raw harness/bridge name to a short display name (or "").
getCompactActivityText(text, max_chars)
    Semantic compression of a long task sentence — never a naive char cut.
tool_row_text(tool, arguments)
    "Pwsh · main.py" style row (same as the DSH web UI).
tool_summary(tool, arguments)
    "运行 pip install…" style capsule line ("verb + object").
formatHarnessActivity(harness, summary)
    "DSH · 检查 DSH 结构…" when a harness name is known, else plain summary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# --------------------------------------------------------------------------- harness name

# Normalised raw name -> short display name (whitespace-insensitive, case-insensitive).
HARNESS_DISPLAY_NAMES = {
    "claude code": "Claude",
    "openai codex cli": "Codex",
    "codex cli": "Codex",
    "deepseek harness": "DSH",
    "deepseek shell harness": "DSH",
    "gemini cli": "Gemini",
    "cursor agent": "Cursor",
}

# Suffixes with no display value, stripped from unknown names.
HARNESS_SUFFIXES = ("harness", "cli", "agent", "bridge", "adapter", "provider")

# After mapping/stripping, tokens that are not a brand -> no harness label at all.
_GENERIC_TOKENS = {
    "",
    "standard",
    "vision",
    "web",
    "desktop",
    "default",
    "node",
    "python",
    "shell",
    "deepseek",
    "openai",
    "gemini",
    "claude",
    "codex",
    "cursor",
    "code",
    "cli",
    "ai",
    "assistant",
}


def _normalise(raw: str) -> str:
    return " ".join((raw or "").lower().split())


def getHarnessDisplayName(rawName: str) -> str:
    """Return a short display name for the harness/codex, or "" if unknown.

    Prefers the explicit mapping table, then strips empty suffixes from the
    unrecognised name; generic/non-brand results are dropped entirely so the
    capsule never shows "standard" or "vision" as a harness.
    """
    key = _normalise(rawName)
    if key in HARNESS_DISPLAY_NAMES:
        return HARNESS_DISPLAY_NAMES[key]
    stripped = key
    for _ in range(3):
        changed = False
        for suffix in HARNESS_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)].strip()
                changed = True
        if not changed:
            break
    if not stripped or stripped in _GENERIC_TOKENS:
        return ""
    return stripped.title()


# ------------------------------------------------------------------------- activity summary

_VOID_FILLER = re.compile(r"^(?:正在|准备|想要|需要|开始|继续(?:进行|深入)?|试图|尝试)\s*")
_CLAUSE_SPLIT = re.compile(
    r"[，,；;。！？!?\n]+|并且|并(?=根据|基于|结合|利用|通过|使用|围绕|朝着|继续|且)|"
    r"然后|再(?!次|来)|接着|之后|随后"
)
# Adverbial prefix ("根据第二张图片调整…") is stripped only up to the first
# action verb, keeping the verb + object of the real activity.
_ADVERB_STRIP = re.compile(
    r"^(?:根据|针对|通过|利用|使用|借助|结合|按照|依据|围绕)"
    r".+?(?=(?:调整|修复|修改|执行|运行|检查|分析|搜索|生成|创建|编写|读取|写入|测试|处理|构建|安装|更新|删除|添加|配置|调用|等待))"
)
_UNIT_FILLER = re.compile(r"(构建|创建|生成|搭建|实现|编写|安装|调整|修复)一个")


def _smart_cut(text: str, limit: int) -> str:
    """Cut long text at a clause/punctuation boundary, never mid-phrase."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for punct in ("，", ",", "；", ";", "：", "、", "：", ")", "）", " "):
        at = cut.rfind(punct)
        if at > limit // 2:
            cut = cut[:at].rstrip()
            break
    return cut


def getCompactActivityText(text: str, max_chars: int = 16) -> str:
    """Compress raw harness activity to a short 动作 + 对象 line (≤16 chars).

    Semantic rules first (waiting / PR search / English verbs / clause
    picking); the final length guard only ever applies at a boundary, so a
    long sentence is never shown as a naive character truncation.
    """
    text = " ".join((text or "").strip().replace("\n", " ").split())
    if not text:
        return ""

    lowered = text.lower()
    # 1) Waiting for the user — highest-value, terse.
    if any(
        frag in lowered
        for frag in (
            "waiting for",
            "awaiting your",
            "wait for user",
        )
    ) or any(frag in text for frag in ("等待你确认", "等你确认", "请你确认", "等待确认")):
        return "等待你确认…"

    # Drop verbose openers up front so later rules see the real action.
    cleaned = _VOID_FILLER.sub("", text.strip())
    cleaned = _UNIT_FILLER.sub(r"\1", cleaned)

    # 2) "调用 GitHub 搜索 DeepGEMM 最近的 Pull Request" -> "检查 DeepGEMM PR…"
    call_search = re.match(r"^\s*调用\s+(.{1,20}?)\s+搜索\s+(.+)$", cleaned)
    if call_search:
        tail = call_search.group(2)
        if "pull request" in tail.lower() or re.search(r"\bPR\b", tail, re.IGNORECASE):
            brand = tail.split()[0].strip("，,。.")
            if brand:
                return f"检查 {brand} PR…"
        head = tail.split()[0].strip("，,。.") if tail else ""
        return f"搜索 {head}…" if head else "搜索…"

    # 3) English verb-led sentences -> Chinese verb + object.
    for en, zh in (
        ("waiting", "等待你确认…"),
        ("searching", "搜索"),
        ("checking", "检查"),
        ("reading", "读取"),
        ("writing", "编写"),
        ("editing", "修改"),
        ("fixing", "修复"),
        ("building", "构建"),
        ("creating", "创建"),
        ("updating", "更新"),
        ("installing", "安装"),
        ("running", "运行"),
        ("executing", "执行"),
        ("analyzing", "分析"),
        ("reviewing", "检查"),
    ):
        match = re.match(rf"^\s*{en}\b(.+)$", lowered)
        if match:
            obj = " ".join(match.group(1).split())
            words = re.split(r"[\s,;()]+", obj)
            filtered = [
                w
                for w in words
                if w
                and w.lower()
                not in ("for", "the", "a", "an", "to", "of", "in", "on", "with", "by", "at", "from")
            ]
            first = filtered[0] if filtered else ""
            if not first:
                return f"{zh}…"
            return f"{zh} {first}…"

    # 4) Chinese sentence: drop fillers, pick the last substantive clause.
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(cleaned) if c.strip()]
    if clauses:
        # Prefer the LAST clause that looks like a concrete action (has a verb
        # head), fall back to the longest clause.
        chosen = None
        for clause in reversed(clauses):
            if len(clause) >= 2 and re.match(r"^[\u4e00-\u9fa5A-Za-z]{2,}", clause):
                chosen = clause
                break
        chosen = chosen or max(clauses, key=len)
        # Drop leading conjunctions left over from the split ("并根据…").
        chosen = re.sub(r"^(?:并且|并|然后|接着|再|随后|之后)[，,；;]?\s*", "", chosen)
        # Strip an adverbial head only when a real verb+object follows.
        adverbial = _ADVERB_STRIP.match(chosen)
        if adverbial is not None and adverbial.end() < len(chosen):
            chosen = chosen[adverbial.end() :].strip()
        if chosen:
            return _smart_cut(chosen, max_chars) + "…"

    return _smart_cut(cleaned or text, max_chars) + "…"


def formatHarnessActivity(harness: str, summary: str) -> str:
    """Wide layout: "DSH · 检查 DSH 结构…"; narrow: just the summary."""
    summary = (summary or "").strip()
    if not harness:
        return summary
    if not summary:
        return harness
    return f"{harness} · {summary}"


def compress_summary_to(summary: str, keep: int = 10) -> str:
    """Tighter capsule variant for a very narrow slot (still boundary-aware)."""
    compact = (summary or "").rstrip("…")
    return _smart_cut(compact, keep) + "…"


# ------------------------------------------------------------------------- tool lines

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
    "list_apps": "computer",
    "get_app_state": "computer",
    "click": "computer",
    "perform_secondary_action": "computer",
    "set_value": "computer",
    "select_text": "computer",
    "scroll": "computer",
    "drag": "computer",
    "press_key": "computer",
    "type_text": "computer",
}
VARIANT_TITLES = {
    "search": "Search",
    "read": "Read",
    "bash": "Bash",
    "write": "Write",
    "edit": "Edit",
    "code": "Code",
    "computer": "Computer Use",
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
    "list_apps": "查看窗口",
    "get_app_state": "观察界面",
    "click": "点击",
    "perform_secondary_action": "操作控件",
    "set_value": "填写",
    "select_text": "选择文本",
    "scroll": "滚动",
    "drag": "拖动",
    "press_key": "按键",
    "type_text": "输入",
}
# Tool display title -> capsule verb ("运行 pip install…").
TOOL_CAPSULE_VERBS = {
    "Search": "搜索",
    "Read": "读取",
    "Bash": "运行",
    "Pwsh": "运行",
    "Write": "写入",
    "Edit": "修改",
    "Code": "执行",
    "Inspect": "检查",
    "Run Cordis Plugin": "运行插件",
    "Stop Cordis Plugin": "停止插件",
    "Remove Cordis Plugin": "移除插件",
    "查看窗口": "查看",
    "观察界面": "观察",
    "点击": "点击",
    "操作控件": "操作",
    "填写": "填写",
    "选择文本": "选择",
    "滚动": "滚动",
    "拖动": "拖动",
    "按键": "按键",
    "输入": "输入",
    "Tool call": "调用工具",
}


def _tool_parts(tool: str, arguments: str = "") -> tuple[str, str]:
    """Return (display title, minimal detail)."""
    variant = TOOL_VARIANTS.get(tool, "others")
    title = TOOL_TITLES.get(tool) or VARIANT_TITLES.get(variant, "Tool call")
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments else {}
    except ValueError:
        args = {}
    detail = ""
    if isinstance(args, dict):
        if tool in {
            "list_apps",
            "get_app_state",
            "click",
            "perform_secondary_action",
            "set_value",
            "select_text",
            "scroll",
            "drag",
            "press_key",
            "type_text",
        }:
            app = str(args.get("app") or "桌面")[:14]
            target = ""
            if tool == "click":
                if args.get("marker") is not None:
                    target = str(args["marker"])
                elif args.get("element_index") is not None:
                    target = f"控件 {args['element_index']}"
                elif args.get("x") is not None and args.get("y") is not None:
                    target = f"{args['x']},{args['y']}"
            elif tool == "press_key" and args.get("key"):
                target = str(args["key"])[:10]
            elif tool == "scroll" and args.get("direction"):
                target = str(args["direction"])[:10]
            detail = " · ".join(part for part in (app, target) if part)
        elif args.get("file_path"):
            detail = Path(str(args["file_path"])).name
        elif args.get("command"):
            parts = re.split(r"[;&|]{1,2}", str(args["command"]))
            last = parts[-1].strip() if parts else ""
            detail = last[:18]
        elif args.get("pattern"):
            detail = str(args["pattern"])[:16]
        elif args.get("query"):
            detail = str(args["query"])[:16]
        elif args.get("path"):
            detail = Path(str(args["path"])).name
        elif args.get("script"):
            detail = str(args["script"])[:16]
    return title, detail


def tool_row_text(tool: str, arguments: str = "") -> str:
    """DSH-style tool row: 'Pwsh · main.py' (official titles, minimal detail)."""
    title, detail = _tool_parts(tool, arguments)
    if not detail:
        return title
    return f"{title} · {detail}"


def tool_summary(tool: str, arguments: str = "") -> str:
    """Capsule line for the active tool: '修改 action/action.json…'."""
    title, detail = _tool_parts(tool, arguments)
    verb = TOOL_CAPSULE_VERBS.get(title, "调用工具")
    if isinstance(arguments, str) and arguments:
        detail = detail.rstrip("…")
    if not detail:
        return f"{verb}…"
    return f"{verb} {detail}…"
