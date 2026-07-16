"""Provider-agnostic LLM client.

Backs any TonerWatch feature that wants to ask an LLM a question:
"What's the OEM SKU for a Kyocera TK-5220 cartridge?", "Summarise
this week's alerts", "Group these printer models by family",
etc.

Supported providers (all optional, chosen via /settings):

* **OpenAI**            — cloud, cheap, wide coverage
* **Azure OpenAI**      — same API, deployed via a specific Azure
                          resource + deployment name; the common
                          MSP choice because it stays inside the
                          tenant's Azure contract
* **Google Gemini**     — cloud, competitive pricing
* **Anthropic Claude**  — cloud
* **Ollama**            — self-hosted, zero-vendor lock-in, private

Every provider is a thin HTTP call — no SDK bloat, no lazy-import
cascade. The response shape is normalised to a plain
``ChatResponse(text=..., model=..., usage={...}, provider=...)``
so callers don't care which vendor answered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from sqlalchemy import func, insert, select, update

from . import crypto, db


logger = logging.getLogger(__name__)

SETTINGS_KEY = "llm"


def _safe_int(v, default: int, lo: int, hi: int) -> int:
    """v0.17.2: form values arrive as strings — clamp instead of crash."""
    try:
        i = int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, i))


def _safe_float(v, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))

PROVIDERS = ("disabled", "openai", "azure_openai", "gemini",
             "anthropic", "ollama")

# Default model names per provider — sensible starting points.
# Operators can override in the settings form.
_DEFAULT_MODEL = {
    "openai":       "gpt-4o-mini",
    "azure_openai": "",  # user must set their deployment name
    "gemini":       "gemini-1.5-flash",
    "anthropic":    "claude-3-5-sonnet-latest",
    "ollama":       "llama3.2",
}


class LLMError(Exception):
    """Raised on any provider-side failure. Caller logs + user-facing."""


@dataclass
class ChatResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, Any]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            select(db.settings.c.value_json)
            .where(db.settings.c.key == SETTINGS_KEY)
        ).first()
    raw = json.loads(row[0]) if row else {}
    if raw.get("api_key_enc"):
        try:
            raw["api_key"] = crypto.decrypt(raw["api_key_enc"])
        except crypto.CryptoError:
            raw["api_key"] = ""
    provider = raw.get("provider", "disabled")
    return {
        "provider":       provider,
        "model":          raw.get("model") or _DEFAULT_MODEL.get(provider, ""),
        "api_key":        raw.get("api_key", ""),
        "endpoint":       raw.get("endpoint", ""),
        "azure_api_version": raw.get("azure_api_version", "2024-06-01"),
        "temperature":    float(raw.get("temperature") or 0.2),
        "max_tokens":     int(raw.get("max_tokens") or 512),
        "api_key_present": bool(raw.get("api_key")),
    }


def save_config(cfg: dict[str, Any]) -> None:
    provider = cfg.get("provider") or "disabled"
    if provider not in PROVIDERS:
        provider = "disabled"
    payload: dict[str, Any] = {
        "provider":          provider,
        "model":             (cfg.get("model") or "").strip(),
        "endpoint":          (cfg.get("endpoint") or "").strip(),
        "azure_api_version": (cfg.get("azure_api_version") or "2024-06-01").strip(),
        "temperature":       _safe_float(cfg.get("temperature"), 0.2, 0.0, 2.0),
        "max_tokens":        _safe_int(cfg.get("max_tokens"), 512, 16, 16384),
    }
    key = cfg.get("api_key") or ""
    if key:
        payload["api_key_enc"] = crypto.encrypt(key)
    else:
        existing = load_config()
        if existing.get("api_key"):
            payload["api_key_enc"] = crypto.encrypt(existing["api_key"])
    _write_settings(payload)


def _write_settings(payload: dict[str, Any]) -> None:
    value_json = json.dumps(payload, ensure_ascii=False)
    with db.get_conn() as conn:
        row = conn.execute(
            db.settings.select().where(db.settings.c.key == SETTINGS_KEY)
        ).first()
        if row is None:
            conn.execute(insert(db.settings).values(
                key=SETTINGS_KEY, value_json=value_json))
        else:
            conn.execute(update(db.settings)
                         .where(db.settings.c.key == SETTINGS_KEY)
                         .values(value_json=value_json,
                                 updated_at=func.current_timestamp()))


def is_configured() -> bool:
    """LLM is available if the provider is enabled and (for cloud
    providers) an API key is stored. Ollama doesn't need a key."""
    cfg = load_config()
    p = cfg["provider"]
    if p == "disabled":
        return False
    if p == "ollama":
        return bool(cfg["endpoint"] or True)  # sensible default: http://localhost:11434
    return bool(cfg["api_key"])


# ---------------------------------------------------------------------------
# Chat completion — the ONE cross-provider primitive
# ---------------------------------------------------------------------------

def chat(system: str, user: str, *,
         config: dict[str, Any] | None = None,
         timeout: float = 30.0) -> ChatResponse:
    """Send a system + user message pair, return the assistant text.

    Doesn't stream — TonerWatch use cases are all short, request/
    response fits comfortably. All providers charged per-token, so
    max_tokens keeps costs bounded.
    """
    cfg = config or load_config()
    p = cfg["provider"]
    if p == "disabled":
        raise LLMError("LLM is disabled in settings")

    if p == "openai":
        return _chat_openai(system, user, cfg, timeout)
    if p == "azure_openai":
        return _chat_azure_openai(system, user, cfg, timeout)
    if p == "gemini":
        return _chat_gemini(system, user, cfg, timeout)
    if p == "anthropic":
        return _chat_anthropic(system, user, cfg, timeout)
    if p == "ollama":
        return _chat_ollama(system, user, cfg, timeout)
    raise LLMError(f"unknown provider: {p!r}")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def _chat_openai(system: str, user: str, cfg: dict[str, Any],
                 timeout: float) -> ChatResponse:
    url = (cfg.get("endpoint") or "https://api.openai.com/v1").rstrip("/") \
          + "/chat/completions"
    return _openai_style_chat(url, cfg, system, user, timeout,
                              provider="openai", auth_bearer=True)


def _chat_azure_openai(system: str, user: str, cfg: dict[str, Any],
                       timeout: float) -> ChatResponse:
    # Azure OpenAI: endpoint = https://<resource>.openai.azure.com
    # + /openai/deployments/<deployment>/chat/completions
    endpoint = (cfg.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise LLMError("Azure OpenAI: endpoint required")
    deployment = cfg.get("model") or ""
    if not deployment:
        raise LLMError("Azure OpenAI: model (=deployment name) required")
    api_version = cfg.get("azure_api_version") or "2024-06-01"
    url = (f"{endpoint}/openai/deployments/{deployment}/chat/completions"
           f"?api-version={api_version}")
    return _openai_style_chat(url, cfg, system, user, timeout,
                              provider="azure_openai", auth_bearer=False,
                              api_key_header="api-key", omit_model=True)


def _openai_style_chat(url: str, cfg: dict[str, Any],
                       system: str, user: str, timeout: float, *,
                       provider: str, auth_bearer: bool,
                       api_key_header: str = "Authorization",
                       omit_model: bool = False) -> ChatResponse:
    key = cfg.get("api_key") or ""
    headers = {"Content-Type": "application/json"}
    if auth_bearer:
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers[api_key_header] = key
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens":  cfg.get("max_tokens", 512),
    }
    if not omit_model:
        body["model"] = cfg.get("model") or "gpt-4o-mini"
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"{provider}: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"{provider}: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return ChatResponse(
        text=text.strip(),
        model=data.get("model") or cfg.get("model") or "",
        provider=provider,
        usage=data.get("usage") or {},
    )


def _chat_gemini(system: str, user: str, cfg: dict[str, Any],
                 timeout: float) -> ChatResponse:
    key = cfg.get("api_key") or ""
    model = cfg.get("model") or "gemini-1.5-flash"
    endpoint = (cfg.get("endpoint")
                or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    # v0.17.1: send the key as a header, not a query parameter. Query-
    # string keys leak into httpx exception messages (which then reach
    # /settings?error=... and the admin's browser history) and into any
    # HTTP proxy log between the app and Google.
    url = f"{endpoint}/models/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": cfg.get("temperature", 0.2),
            "maxOutputTokens": cfg.get("max_tokens", 512),
        },
    }
    try:
        r = httpx.post(url,
                       headers={"x-goog-api-key": key} if key else {},
                       json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"gemini: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"gemini: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    parts = ((data.get("candidates") or [{}])[0]
             .get("content", {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return ChatResponse(
        text=text, model=model, provider="gemini",
        usage=data.get("usageMetadata") or {},
    )


def _chat_anthropic(system: str, user: str, cfg: dict[str, Any],
                    timeout: float) -> ChatResponse:
    key = cfg.get("api_key") or ""
    model = cfg.get("model") or "claude-3-5-sonnet-latest"
    url = (cfg.get("endpoint") or "https://api.anthropic.com/v1").rstrip("/") \
          + "/messages"
    body = {
        "model": model,
        "max_tokens": cfg.get("max_tokens", 512),
        "temperature": cfg.get("temperature", 0.2),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"anthropic: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"anthropic: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    return ChatResponse(
        text=text, model=model, provider="anthropic",
        usage=data.get("usage") or {},
    )


def _chat_ollama(system: str, user: str, cfg: dict[str, Any],
                 timeout: float) -> ChatResponse:
    model = cfg.get("model") or "llama3.2"
    endpoint = (cfg.get("endpoint") or "http://localhost:11434").rstrip("/")
    url = f"{endpoint}/api/chat"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": cfg.get("temperature", 0.2),
            "num_predict":  cfg.get("max_tokens", 512),
        },
    }
    try:
        r = httpx.post(url, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"ollama: {e.__class__.__name__}: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"ollama: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = (data.get("message") or {}).get("content", "").strip()
    return ChatResponse(
        text=text, model=model, provider="ollama",
        usage={"total_duration": data.get("total_duration")},
    )
