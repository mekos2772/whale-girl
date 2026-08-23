"""Chain-of-thought (reasoning) summarizer for the DSH message bar.

Uses the same model DSH is configured with — ``agent-default-model`` in
``~/.dsh/settings.yaml`` (currently opencode-go / deepseek-v4-flash) — through
the provider's OpenAI-compatible endpoint. The model/provider can be switched
at runtime via ``configure()`` and persisted to a small sidecar JSON.

Pure stdlib (urllib), so domain tests run without extra dependencies and the
pet never blocks: calls are made from a worker thread by the integration.

Provider resolution order (first hit wins):
  1. pi-ai model catalog JSON next to the DSH install (full model data).
  2. ``~/.dsh/settings.yaml`` ``llm-pi-ai.providers.<id>`` block (baseURL/api).
  3. built-in fallback registry (opencode-go).
API keys come from ``~/.dsh/.credentials.yaml`` (``<PROVIDER>_API_KEY`` or the
provider block's ``apiKeyEnv``).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

try:  # PyYAML is present in this environment; optional by design.
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover - fallback parser below
    _yaml = None

DEFAULT_DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
DEFAULT_GLOBAL_DSH = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "npm"
    / "node_modules"
    / "@deepseek-ai"
    / "dsh"
)
PI_AI_DATA = Path(
    os.environ.get(
        "MIMI_PI_AI_DATA",
        str(DEFAULT_GLOBAL_DSH / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "providers" / "data"),
    )
)
DEFAULT_SETTINGS_PATH = DEFAULT_DSH_HOME / "settings.yaml"
DEFAULT_CREDENTIALS_PATH = DEFAULT_DSH_HOME / ".credentials.yaml"

# Fallback registry used when neither the pi-ai catalog nor settings describe
# the provider. baseURL/protocol verified against the pi-ai catalog.
BUILTIN_PROVIDERS: dict[str, dict] = {
    "opencode-go": {
        "base": "https://opencode.ai/zen/go/v1",
        "protocol": "openai-completions",
        "models": [
            "deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2", "kimi-k3",
            "mimo-v2.5", "minimax-m2.7", "qwen3.6-plus",
        ],
    },
}

CONCLUSION_MARKERS = ("结论", "所以", "因此", "综上", "最终", "决定", "采用", "选择", "方案")

SYSTEM_PROMPT = (
    "你是一个思维链总结器。把用户给出的思考过程压缩成一句中文摘要，"
    "保留结论和关键决策，去掉过程细节。不超过80字。直接输出摘要，不要任何前缀。"
)


# --------------------------------------------------------------------------- yaml

def _parse_yaml_text(text: str) -> dict:
    """Parse YAML if PyYAML is available, else a tiny indentation parser.

    The tiny parser understands the subset used by DSH settings/credentials:
    nested mappings, scalars, lists of scalars, inline comments.
    """
    if _yaml is not None:
        value = _yaml.safe_load(text)
        return value if isinstance(value, dict) else {}
    result: dict = {}
    stack: list[tuple[int, dict]] = [(-1, result)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line.lstrip(" ").startswith("- "):
            item = line.lstrip(" ")[2:].strip()
            parent = stack[-1][1]
            if isinstance(parent, list):
                parent.append(item)
            else:
                parent.setdefault("__list__", []).append(item)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip().strip("'\"")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value:
            parent[key] = value
        else:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
    return result


def load_yaml(path: Path) -> dict:
    try:
        if path.exists():
            return _parse_yaml_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    return {}


# ------------------------------------------------------------------ DSH config

def load_agent_default_model(settings: dict | None = None) -> tuple[str, str]:
    """(provider, model) from DSH settings' agent-default-model."""
    settings = settings if settings is not None else load_yaml(DEFAULT_SETTINGS_PATH)
    block = settings.get("agent-default-model") or {}
    return str(block.get("provider", "opencode-go")), str(block.get("model", "deepseek-v4-flash"))


def load_provider_settings(settings: dict | None = None) -> dict:
    settings = settings if settings is not None else load_yaml(DEFAULT_SETTINGS_PATH)
    providers = {}
    for entry in (settings.get("llm-pi-ai") or {}).get("providers") or {}:
        providers[entry] = (settings["llm-pi-ai"]["providers"][entry] or {})
    return providers


def load_api_keys(credentials: dict | None = None) -> dict:
    credentials = credentials if credentials is not None else load_yaml(DEFAULT_CREDENTIALS_PATH)
    return {str(k): str(v) for k, v in credentials.items()}


def _catalog_lookup(provider: str) -> dict | None:
    """Look up the provider in the pi-ai model catalog JSON next to DSH."""
    try:
        path = PI_AI_DATA / f"{provider}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        # Prefer OpenAI-compatible protocols (we POST chat/responses ourselves).
        for protocol in ("openai-completions", "openai-responses", "anthropic-messages"):
            models = data.get(protocol)
            if not models:
                continue
            for model_id, info in models.items():
                if isinstance(info, dict) and info.get("baseUrl"):
                    return {
                        "base": info["baseUrl"],
                        "protocol": protocol,
                        "models": sorted(str(m) for m in models),
                    }
    except (OSError, ValueError):
        return None
    return None


def resolve_provider_spec(provider: str) -> dict:
    """Resolve {base, protocol, models} for a provider id, plus api key name."""
    spec = _catalog_lookup(provider) or dict(BUILTIN_PROVIDERS.get(provider) or {})
    settings_providers = load_provider_settings()
    if provider in settings_providers:
        block = settings_providers[provider]
        if block.get("baseURL"):
            spec.setdefault("base", block["baseURL"])
        if block.get("api"):
            spec.setdefault("protocol", block["api"])
        models = block.get("models")
        if isinstance(models, list) and models:
            spec["models"] = [str(m.get("id", m)) if isinstance(m, dict) else str(m) for m in models]
        if block.get("apiKeyEnv"):
            spec["api_key_env"] = block["apiKeyEnv"]
    spec.setdefault("api_key_env", f"{provider.upper().replace('-', '_')}_API_KEY")
    return spec


# ------------------------------------------------------------------ summarization

def local_summary(text: str, max_chars: int = 120) -> str:
    """Heuristic fallback: prefer conclusion-carrying sentences, truncate."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned

    def marker_pos(value: str) -> int:
        return min(
            (value.find(marker) for marker in CONCLUSION_MARKERS if value.find(marker) != -1),
            default=-1,
        )

    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", cleaned) if s.strip()]
    key = [s for s in sentences if any(m in s for m in CONCLUSION_MARKERS)]
    if key:
        base = "".join(key)
    else:
        first = marker_pos(cleaned)
        base = cleaned[max(0, first - 24):] if first >= 0 else cleaned
    base = " ".join(base.split())
    if len(base) > max_chars:
        # Re-window around the conclusion marker so truncation keeps it.
        first = marker_pos(base)
        if first >= 0 and first + 8 > max_chars:
            base = base[max(0, first - 24):]
    if len(base) <= max_chars:
        return base
    return base[:max_chars].rstrip("，, ") + "…"


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _http_json(url: str, body: dict, api_key: str, timeout: float = 20.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": BROWSER_UA,  # gateways filter non-browser clients
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CoTSummarizer:
    """Summarizes reasoning chains via the DSH-configured LLM endpoint.

    ``provider``/``model`` default to "auto": follow DSH's agent-default-model.
    """

    def __init__(self, settings_path: Path | None = None,
                 credentials_path: Path | None = None) -> None:
        self.settings_path = settings_path or DEFAULT_SETTINGS_PATH
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self.provider: str | None = "auto"
        self.model: str | None = None
        self.enabled = True
        self.last_error: str | None = None

    # ------------------------------------------------------------------ config

    def configure(self, provider: str | None, model: str | None = None) -> None:
        self.provider = provider  # "auto" follows DSH's agent default
        self.model = model

    def model_choices(self) -> list[tuple[str, str, str]]:
        """(label, provider, model) options: auto + resolved provider's models."""
        choices = [("自动（跟随 DSH）", "auto", "")]
        settings = load_yaml(self.settings_path)
        provider, model = load_agent_default_model(settings)
        spec = resolve_provider_spec(provider)
        for m in spec.get("models") or []:
            choices.append((f"{provider} / {m}", provider, m))
        if self.model and self.model not in [c[2] for c in choices]:
            choices.append((f"{self.provider} / {self.model}", str(self.provider), self.model))
        return choices

    # ------------------------------------------------------------------ call

    def summarize(self, text: str, max_chars: int = 120) -> str:
        """Return a short Chinese summary; falls back to local heuristics."""
        text = " ".join(str(text or "").split())
        if not text:
            return ""
        spec = self._resolve_spec()
        if spec is None:
            return local_summary(text, max_chars)
        try:
            return self._llm_summarize(spec, text, max_chars)
        except Exception as exc:  # network / protocol / key errors -> heuristic
            self.last_error = str(exc)
            return local_summary(text, max_chars)

    def _resolve_spec(self) -> dict | None:
        settings = load_yaml(self.settings_path)
        credentials = load_api_keys(load_yaml(self.credentials_path))
        if self.provider == "auto" or not self.provider:
            provider, model = load_agent_default_model(settings)
        else:
            provider = self.provider
            model = self.model or ""
        if not provider:
            return None
        spec = resolve_provider_spec(provider)
        key_name = spec.get("api_key_env", f"{provider.upper().replace('-', '_')}_API_KEY")
        api_key = credentials.get(key_name, "")
        if not api_key and provider == "auto":
            return None
        if not api_key:
            return None
        base = str(spec.get("base", "")).rstrip("/")
        if not base:
            return None
        spec["provider"] = provider
        spec["model"] = model or str((spec.get("models") or ["deepseek-v4-flash"])[0])
        spec["api_key"] = api_key
        return spec

    def _llm_summarize(self, spec: dict, text: str, max_chars: int) -> str:
        model = spec["model"]
        base = spec["base"]
        protocol = spec.get("protocol", "openai-completions")
        api_key = spec["api_key"]
        started = time.time()
        if protocol == "openai-responses":
            url = f"{base}/responses"
            body = {
                "model": model,
                "input": [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\n{text}"}],
                "max_output_tokens": 256,
            }
            data = _http_json(url, body, api_key)
            parts = []
            for output in data.get("output") or []:
                for content in (output.get("content") or []):
                    if content.get("type") == "output_text":
                        parts.append(content.get("text", ""))
            summary = "".join(parts)
        else:
            url = f"{base}/chat/completions"
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 256,
                "temperature": 0.2,
            }
            data = _http_json(url, body, api_key)
            summary = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        summary = " ".join(str(summary or "").split()).strip()
        if len(summary) > max_chars:
            summary = summary[:max_chars].rstrip("，, ") + "…"
        return summary or local_summary(text, max_chars)
