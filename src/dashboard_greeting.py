"""AI-personalized dashboard greeting — v0.24.13.

Replaces the static "N printers · N customers, N critical" sentence
with an LLM-phrased one when an LLM is configured, built from the same
facts the static sentence already uses plus recent anomaly events
(v0.24.5) so it can call out a specific situation ("Acme GmbH keeps
tripping the toner-level anomaly check") instead of just totals.

Generated at most once per hour per (user, language) pair (in-memory,
process-local, same TTL-cache pattern as bi_client's supply cache) —
an LLM call on every dashboard load would trade a <100ms static
render for 1-3s of added latency and a real per-request cost, for a
sentence that rarely changes meaningfully within an hour. Language is
part of the cache key (not just the user id) — otherwise switching
the UI language kept showing the greeting generated in whatever
language happened to hit the LLM first, for up to an hour. Any
failure (LLM disabled, call error, timeout) falls back to the static
sentence — the greeting is decoration, never a dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 3600
_CACHE_LOCK = threading.Lock()
# (user_id, lang) -> (cached_at, facts_hash, text). v0.24.45: the key
# used to be user_id alone — a user who switched UI language kept
# seeing the greeting generated in whatever language happened to hit
# the LLM first, for up to an hour, regardless of their current
# choice. lang is part of the cache identity now, same as facts_hash.
_CACHE: dict[tuple[int, str], tuple[float, int, str]] = {}


def _facts_hash(counts: dict, urgent_names: list[str],
                problem_customers: list[dict], anomalies: list[dict]) -> int:
    """Cheap fingerprint of the facts that would change the greeting —
    lets a real change (new critical customer, fresh anomaly) bust the
    hourly cache early instead of showing stale praise on a bad day."""
    return hash((
        counts.get("customers"), counts.get("printers"),
        counts.get("critical"), counts.get("warn"),
        tuple(urgent_names),
        tuple((c.get("name"), c.get("critical"), c.get("warn"))
              for c in problem_customers),
        tuple(a.get("id") for a in anomalies),
    ))


def generate_greeting(user_id: int, user_name: str, counts: dict,
                       urgent_names: list[str], anomalies: list[dict],
                       lang: str = "de",
                       problem_customers: list[dict] | None = None) -> str | None:
    """Return an AI-phrased greeting sentence, or ``None`` if the LLM
    isn't configured, the call fails, or it takes too long — callers
    fall back to the static sentence in every one of those cases.

    ``problem_customers`` — the (already urgency-sorted) customers
    with a critical or warn supply, each as {"name", "critical",
    "warn"} — lets the model name specific trouble spots with real
    counts instead of just totals. Optional for backward compatibility;
    pass [] to disable that detail."""
    from . import llm_client
    if not llm_client.is_configured():
        return None
    problem_customers = problem_customers or []

    fhash = _facts_hash(counts, urgent_names, problem_customers, anomalies)
    cache_key = (user_id, lang)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[1] == fhash and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[2]

    lang_name = {"de": "German", "en": "English", "fr": "French",
                 "it": "Italian", "es": "Spanish",
                 "nl": "Dutch"}.get(lang, "English")
    system = (
        "You write a short, warm greeting for the top of a print-"
        "supply monitoring dashboard, addressed to the logged-in "
        f"operator by name. Write in {lang_name}. Use ONLY the facts "
        "given below — never invent a number, a customer name, or an "
        "event that isn't listed. "
        "If there's nothing wrong (no critical, no warn, no "
        "anomalies), say so plainly and warmly in ONE sentence — "
        "don't invent tension. "
        "If there IS something wrong, write TWO short sentences: the "
        "first a brief warm greeting, the second naming the specific "
        "customers with problems and their exact critical/warn counts "
        "from 'problem_customers' (not just a total) — e.g. 'Acme has "
        "3 critical and 1 warn, Beta has 2 warn.' If 'recent_anomalies' "
        "has a notable entry (an unusual jump, not a normal cartridge "
        "swap), you may fold that in too if it fits naturally. "
        "Plain prose, no markdown, no emoji, no sign-off, no bullet "
        "list — flowing sentences only."
    )
    import json as _json
    facts = {
        "operator_name": user_name,
        "customers": counts.get("customers"),
        "printers": counts.get("printers"),
        "critical": counts.get("critical"),
        "warn": counts.get("warn"),
        "customers_needing_attention": urgent_names,
        "problem_customers": [
            {"name": c.get("name"), "critical": c.get("critical"),
             "warn": c.get("warn")}
            for c in problem_customers
        ],
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
        _CACHE[cache_key] = (now, fhash, text)
    return text
