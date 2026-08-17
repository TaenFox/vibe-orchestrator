from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


TOKEN_USAGE_SOURCE = "codex_cli.turn.completed"


def unknown_token_usage() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "source": "unknown",
        "captured_at": None,
    }


def _captured_at(value: Any, fallback: str | None) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return fallback or datetime.now(timezone.utc).isoformat()


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_codex_usage(events: str | bytes, *, captured_at: str | None = None) -> dict[str, Any]:
    """Parse only the supported Codex CLI JSONL usage event.

    Unknown, malformed, legacy, or incomplete output deliberately produces an
    unknown value; no token count is inferred from text, result fields, or
    other event shapes.
    """
    if isinstance(events, bytes):
        events = events.decode("utf-8", errors="replace")
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not (_non_negative_int(input_tokens) and _non_negative_int(output_tokens)):
            continue
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "source": TOKEN_USAGE_SOURCE,
            "captured_at": _captured_at(event.get("timestamp"), captured_at),
        }
    return unknown_token_usage()


def is_confirmed_token_usage(usage: Any) -> bool:
    return (
        isinstance(usage, dict)
        and usage.get("source") == TOKEN_USAGE_SOURCE
        and _non_negative_int(usage.get("input_tokens"))
        and _non_negative_int(usage.get("output_tokens"))
        and _non_negative_int(usage.get("total_tokens"))
        and isinstance(usage.get("captured_at"), str)
    )

