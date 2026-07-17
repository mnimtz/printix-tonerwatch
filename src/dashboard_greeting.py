"""AI-personalized dashboard greeting — v0.24.13.

Replaces the static "N printers · N customers, N critical" sentence
with an LLM-phrased one when an LLM is configured, built from the same
facts the static sentence already uses plus recent anomaly events
(v0.24.5) so it can call out a specific situation ("Acme GmbH keeps
tripping the toner-level anomaly check") instead of just totals.

Generated at most once per hour per user (in-memory, process-local,
same TTL-cache pattern as bi_client's supply cache) — an LLM call on
every dashboard load would trade a <100ms static render for 1-3s of
added latency and a real per-request cost, for a sentence that rarely
changes meaningfully within an hour. Any failure (LLM disabled, call
error, timeout) falls back to the static sentence — the greeting is
decoration, never a dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 3600
_CACHE_LOCK = threading.Lock()
_CACHE: dict[int, tuple[float, int, str]] = {}  # user_id -> (cached_at, facts_hash, text)


def _facts_hash(counts: dict, urgent_names: list[str], anomalies: list[dict]) -> int:
    """Cheap fingerprint of the facts that would change the greeting —
    lets a real change (new critical customer, fresh anomaly) bust the
    hourly cache early instead of showing stale praise on a bad day."""
    return hash((
        counts.get("customers"), counts.get("printers"),
        counts.get("critical"), counts.get("warn"),
        tuple(urgent_names),
        tuple(a.get("id") for a in anomalies),
    ))


def generate_greeting(user_id: int, user_name: str, counts: dict,
                       urgent_names: list[str], anomalies: list[dict],
                       lang: str = "de") -> str | None:
    """Return an AI-phrased greeting sentence, or ``None`` if the LLM
    isn't configured, the call fails, or it takes too long — callers
    fall back to the static sentence in every one of those cases."""
    from . import llm_client
    if not llm_client.is_configured():
        return None

    fhash = _facts_hash(counts, urgent_names, anomalies)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(user_id)
        if cached and cached[1] == fhash and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[2]

    lang_name = {"de": "German", "en": "English", "fr": "French",
                 "it": "Italian", "es": "Spanish"}.get(lang, "English")
    system = (
        "You write a single short, warm greeting sentence for the top "
        "of a print-supply monitoring dashboard, addressed to the "
        f"logged-in operator by name. Write in {lang_name}. Use ONLY "
        "the facts given below — never invent a number, a customer "
        "name, or an event that isn't listed. If there are anomalies "
        "listed, you may mention the most notable one by customer "
        "name. If everything is fine (no critical, no warn, no "
        "anomalies), say so plainly and warmly — don't invent tension. "
        "One sentence, plain prose, no markdown, no emoji, no sign-off."
    )
    import json as _json
    facts = {
        "operator_name": user_name,
        "customers": counts.get("customers"),
        "printers": counts.get("printers"),
        "critical": counts.get("critical"),
        "warn": counts.get("warn"),
        "customers_needing_attention": urgent_names,
        "recent_anomalies": [
            {"customer": a.get("customer_name"), "printer_id": a.get("printer_id"),
             "color": a.get("color"),
             "kind": (a.get("meta") or {}).get("kind"),
             "prev_level": (a.get("meta") or {}).get("prev_level"),
             "level": a.get("level")}
            for a in anomalies
        ],
    }
    user = "Facts (JSON): " + _json.dumps(facts)
    try:
        resp = llm_client.chat(system, user, timeout=8.0)
    except llm_client.LLMError as e:
        logger.info("[dashboard_greeting] LLM error for user %s: %s", user_id, e)
        return None
    except Exception as e:  # noqa: BLE001 — a slow/broken provider must never break the dashboard
        logger.warning("[dashboard_greeting] unexpected error for user %s: %s", user_id, e)
        return None

    text = (resp.text or "").strip()
    if not text:
        return None

    with _CACHE_LOCK:
        _CACHE[user_id] = (now, fhash, text)
    return text
