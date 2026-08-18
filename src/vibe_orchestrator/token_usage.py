from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Iterable


TOKEN_USAGE_SOURCE = "provider"
NORMALIZATION_VERSION = "tokens_per_1000.v1"
CODEX_FALLBACK_POLICY_VERSION = "codex_cli_0.147.0_turn_completed.v1"


def unknown_token_usage(*, run_id: str | None = None, model: str | None = None,
                        reasoning_effort: str | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "source": "unknown",
        "usage_ref": None,
        "captured_at": None,
        "normalization_version": None,
    }


def _captured_at(value: Any, fallback: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback.strip() if isinstance(fallback, str) and fallback.strip() else None


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _event_value(event: dict[str, Any], usage: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in event:
            return event[name]
        if name in usage:
            return usage[name]
    return None


def _legacy_parse(events: str, captured_at: str | None) -> dict[str, Any]:
    """Read pre-contract artifacts; new runner calls never use this path."""
    input_total = output_total = 0
    latest = None
    found = False
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        usage = event.get("usage") if isinstance(event, dict) and event.get("type") == "turn.completed" else None
        if not isinstance(usage, dict) or not (_non_negative_int(usage.get("input_tokens")) and _non_negative_int(usage.get("output_tokens"))):
            continue
        found = True
        input_total += usage["input_tokens"]
        output_total += usage["output_tokens"]
        latest = _captured_at(event.get("timestamp"), latest)
    if not found:
        return unknown_token_usage()
    return {
        "input_tokens": input_total,
        "output_tokens": output_total,
        "total_tokens": input_total + output_total,
        "source": "codex_cli.turn.completed",
        "captured_at": latest or captured_at,
    }


def _runner_fallback_parse(
    events: str,
    *,
    runner_run_id: str,
    model: str,
    reasoning_effort: str,
    captured_at: str | None,
) -> dict[str, Any]:
    """Adapt the minimal Codex CLI 0.147.0 event to the runner contract.

    The CLI event has provider-origin counters but no trustworthy correlation
    metadata.  The runner supplies that metadata.  This deliberately accepts
    one distinct normalized event only: without provider semantics, combining
    multiple events could turn cumulative snapshots into an overcount.
    """
    if not all(isinstance(value, str) and value.strip() for value in (
        runner_run_id, model, reasoning_effort,
    )):
        return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
    if not isinstance(captured_at, str) or not captured_at.strip():
        return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)

    events_by_payload: dict[str, dict[str, Any]] = {}
    for line in events.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if not (_non_negative_int(input_tokens) and _non_negative_int(output_tokens)):
            return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
        total = usage.get("total_tokens", input_tokens + output_tokens)
        if not _non_negative_int(total) or total != input_tokens + output_tokens:
            return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
        normalized = {
            "type": "turn.completed",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total},
            "provider_event_id": _event_value(event, usage, "provider_event_id"),
            "provider_request_id": _event_value(event, usage, "provider_request_id"),
        }
        payload_hash = sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        events_by_payload[payload_hash] = {
            "event": event, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": total, "payload_hash": payload_hash,
        }

    if len(events_by_payload) != 1:
        return unknown_token_usage(run_id=runner_run_id, model=model, reasoning_effort=reasoning_effort)
    item = next(iter(events_by_payload.values()))
    event = item["event"]
    event_timestamp = _captured_at(event.get("timestamp"), captured_at)
    usage_ref = f"{CODEX_FALLBACK_POLICY_VERSION}:1:{item['payload_hash']}"
    return {
        "run_id": runner_run_id,
        "input_tokens": item["input_tokens"],
        "output_tokens": item["output_tokens"],
        "total_tokens": item["total_tokens"],
        "model": model,
        "reasoning_effort": reasoning_effort,
        "source": "runner_fallback",
        "usage_ref": usage_ref,
        "captured_at": event_timestamp,
        "normalization_version": NORMALIZATION_VERSION,
        "fallback_policy_version": CODEX_FALLBACK_POLICY_VERSION,
        "degraded_confidence": True,
        "provider_event_id": _event_value(event, event.get("usage", {}), "provider_event_id"),
        "provider_request_id": _event_value(event, event.get("usage", {}), "provider_request_id"),
    }


def parse_codex_usage(
    events: str | bytes,
    *,
    expected_run_id: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    captured_at: str | None = None,
    runner_run_id: str | None = None,
    profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Parse provider-confirmed usage, failing closed on weak evidence.

    A contract-aware call requires exact run/profile correlation, a stable
    usage_ref and explicit cumulative/incremental semantics.  The no-profile
    path remains solely for reading old artifacts and is intentionally not a
    confirmed usage contract.
    """
    if isinstance(events, bytes):
        events = events.decode("utf-8", errors="replace")
    if runner_run_id is not None or profile is not None:
        runner_profile = profile or {}
        return _runner_fallback_parse(
            events,
            runner_run_id=runner_run_id or expected_run_id or "",
            model=runner_profile.get("model", model or ""),
            reasoning_effort=runner_profile.get("reasoning_effort", reasoning_effort or ""),
            captured_at=captured_at,
        )
    if expected_run_id is None and model is None and reasoning_effort is None:
        return _legacy_parse(events, captured_at)

    candidates: list[dict[str, Any]] = []
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
        run_value = _event_value(event, usage, "run_id", "execution_run_id")
        event_model = _event_value(event, usage, "model")
        event_reasoning = _event_value(event, usage, "reasoning_effort")
        usage_ref = _event_value(event, usage, "usage_ref")
        if not isinstance(usage_ref, str) or not usage_ref.strip():
            usage_ref = _event_value(event, usage, "provider_event_id")
        semantics = _event_value(event, usage, "usage_semantics")
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if (run_value != expected_run_id or event_model != model or event_reasoning != reasoning_effort
                or not isinstance(usage_ref, str) or not usage_ref.strip()
                or semantics not in {"incremental", "cumulative"}
                or not (_non_negative_int(input_tokens) and _non_negative_int(output_tokens))):
            continue
        event_captured_at = _captured_at(event.get("timestamp"), captured_at)
        if event_captured_at is None:
            continue
        total = usage.get("total_tokens", input_tokens + output_tokens)
        if not _non_negative_int(total) or total != input_tokens + output_tokens:
            continue
        candidates.append({
            "run_id": expected_run_id, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": total, "model": model, "reasoning_effort": reasoning_effort,
            "usage_ref": usage_ref.strip(), "usage_semantics": semantics,
            "captured_at": event_captured_at,
            "provider_event_id": _event_value(event, usage, "provider_event_id"),
            "provider_request_id": _event_value(event, usage, "provider_request_id"),
        })
    if not candidates:
        return unknown_token_usage(run_id=expected_run_id, model=model, reasoning_effort=reasoning_effort)

    semantics = {item["usage_semantics"] for item in candidates}
    if len(semantics) != 1:
        return unknown_token_usage(run_id=expected_run_id, model=model, reasoning_effort=reasoning_effort)
    if next(iter(semantics)) == "incremental":
        unique: dict[str, dict[str, Any]] = {}
        for item in candidates:
            previous = unique.get(item["usage_ref"])
            if previous is not None and (previous["input_tokens"], previous["output_tokens"]) != (item["input_tokens"], item["output_tokens"]):
                return unknown_token_usage(run_id=expected_run_id, model=model, reasoning_effort=reasoning_effort)
            unique[item["usage_ref"]] = item
        selected = list(unique.values())
        input_total = sum(item["input_tokens"] for item in selected)
        output_total = sum(item["output_tokens"] for item in selected)
    else:
        ordered = list(candidates)
        seen_counts: dict[str, tuple[int, int]] = {}
        for item in ordered:
            counts = (item["input_tokens"], item["output_tokens"])
            previous_counts = seen_counts.get(item["usage_ref"])
            if previous_counts is not None and previous_counts != counts:
                return unknown_token_usage(run_id=expected_run_id, model=model, reasoning_effort=reasoning_effort)
            seen_counts[item["usage_ref"]] = counts
        for previous, current in zip(ordered, ordered[1:]):
            if current["input_tokens"] < previous["input_tokens"] or current["output_tokens"] < previous["output_tokens"]:
                return unknown_token_usage(run_id=expected_run_id, model=model, reasoning_effort=reasoning_effort)
        latest = ordered[-1]
        input_total, output_total = latest["input_tokens"], latest["output_tokens"]
        selected = [latest]
    latest = selected[-1]
    return {
        "run_id": expected_run_id, "input_tokens": input_total, "output_tokens": output_total,
        "total_tokens": input_total + output_total, "model": model, "reasoning_effort": reasoning_effort,
        "source": TOKEN_USAGE_SOURCE, "usage_ref": latest["usage_ref"],
        "captured_at": latest["captured_at"], "normalization_version": NORMALIZATION_VERSION,
        "usage_semantics": latest["usage_semantics"],
        "provider_event_id": latest.get("provider_event_id"),
        "provider_request_id": latest.get("provider_request_id"),
    }


def is_confirmed_token_usage(usage: Any, *, run_id: str | None = None,
                             model: str | None = None, reasoning_effort: str | None = None) -> bool:
    if not isinstance(usage, dict) or usage.get("source") not in {"provider", "runner_fallback"}:
        return False
    if not all(isinstance(usage.get(field), str) and usage[field].strip() for field in (
        "run_id", "model", "reasoning_effort", "usage_ref", "captured_at", "normalization_version",
    )):
        return False
    if run_id is not None and usage.get("run_id") != run_id:
        return False
    if model is not None and usage.get("model") != model:
        return False
    if reasoning_effort is not None and usage.get("reasoning_effort") != reasoning_effort:
        return False
    if not (_non_negative_int(usage.get("input_tokens")) and _non_negative_int(usage.get("output_tokens"))
            and usage.get("total_tokens") == usage["input_tokens"] + usage["output_tokens"]
            and usage["usage_ref"].strip()):
        return False
    if usage.get("source") == "provider":
        return isinstance(usage.get("normalization_version"), str) and bool(usage["normalization_version"])
    return (usage.get("degraded_confidence") is True
            and isinstance(usage.get("fallback_policy_version"), str) and bool(usage["fallback_policy_version"])
            and isinstance(usage.get("normalization_version"), str) and bool(usage["normalization_version"]))
