"""Parse Codex CLI JSONL session transcripts into structured events."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1
_INHERITANCE_TOLERANCE_SECONDS = 3.0

_EVENT_KINDS = {
    "agent_message",
    "agent_reasoning",
    "context_compacted",
    "image_generation_end",
    "item_completed",
    "mcp_tool_call_end",
    "patch_apply_begin",
    "patch_apply_end",
    "patch_apply_start",
    "sub_agent_activity",
    "task_complete",
    "task_started",
    "thread_rolled_back",
    "thread_settings_applied",
    "token_count",
    "turn_aborted",
    "user_message",
    "web_search_begin",
    "web_search_end",
}

_OUTER_KINDS = {
    "compacted",
    "inter_agent_communication_metadata",
    "session_meta",
    "turn_context",
    "world_state",
}

_RESPONSE_KINDS = {
    "agent_message": "agent_message",
    "compaction": "compacted",
    "custom_tool_call": "tool_call",
    "custom_tool_call_output": "tool_output",
    "function_call": "tool_call",
    "function_call_output": "tool_output",
    "image_generation_call": "tool_call",
    "message": "message",
    "reasoning": "reasoning",
    "tool_search_call": "tool_call",
    "tool_search_call_output": "tool_output",
    "web_search_call": "tool_call",
}


def _as_text(value: Any) -> str:
    """Normalize possibly-null payload fields to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def parse_jsonl(path: str | Path) -> list[dict]:
    """Read a JSONL file and return a list of parsed JSON objects."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def load_session(
    path: str | Path,
    include_inherited: bool = False,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Load a transcript into the lossless normalized schema.

    Invalid JSON lines are retained as ``parse_error`` events. Subagent logs
    omit their copied parent prefix unless ``include_inherited`` is true.
    """
    source = str(Path(path))
    records = list(_read_records(path))
    result = _normalize_records(
        records,
        source=source,
        include_inherited=include_inherited,
        include_raw=include_raw,
    )
    return result


def iter_normalized(
    path: str | Path,
    include_inherited: bool = False,
    include_raw: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield normalized events without retaining the transcript in memory.

    A constant-memory prepass finds session identity and the subagent boundary,
    then events are streamed. Tool outputs carry ``paired_seq`` when their
    matching call has already appeared; both sides carry the stable ``call_id``.
    """
    source = str(Path(path))
    meta_record, boundary = _scan_session(path)
    yield from _iter_events(
        _read_records(path),
        meta_record=meta_record,
        boundary=boundary,
        source=source,
        include_inherited=include_inherited,
        include_raw=include_raw,
    )


def normalize_entries(
    entries: Iterable[Any],
    source: str = "<memory>",
    include_inherited: bool = True,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Normalize already-parsed entries using the same schema as load_session."""
    records = [(line, entry, None) for line, entry in enumerate(entries, 1)]
    return _normalize_records(
        records,
        source=source,
        include_inherited=include_inherited,
        include_raw=include_raw,
    )


def viewer_projection(
    session: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Project normalized events into the compact legacy viewer event model."""
    projected: list[dict[str, Any]] = []
    turn_seq = 0
    for event in session.get("events", []):
        if event.get("kind") == "task_started":
            turn_seq += 1
        raw = event.get("raw")
        if isinstance(raw, dict):
            payload = raw.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if raw.get("type") == "event_msg":
                before = len(projected)
                _handle_event_msg(payload, event.get("timestamp", ""), projected, turn_seq)
                if len(projected) > before:
                    continue
            if raw.get("type") == "response_item":
                before = len(projected)
                _handle_response_item(payload, event.get("timestamp", ""), projected, turn_seq)
                if len(projected) > before:
                    continue
                if payload.get("role") in {"developer", "system"}:
                    continue
        fallback = _project_normalized_event(event, turn_seq)
        if fallback is not None:
            projected.append(fallback)

    reconciled = _reconcile_events(projected)
    return session.get("meta") or {}, [
        _strip_internal_keys(event) for event in reconciled
    ]


def _read_records(path: str | Path) -> Iterator[tuple[int, Any, str | None]]:
    with open(path, encoding="utf-8") as transcript:
        for line_number, line in enumerate(transcript, 1):
            raw_text = line.rstrip("\r\n")
            if not raw_text.strip():
                continue
            try:
                yield line_number, json.loads(raw_text), None
            except json.JSONDecodeError as error:
                yield line_number, raw_text, error.msg


def _normalize_records(
    records: Iterable[tuple[int, Any, str | None]],
    *,
    source: str,
    include_inherited: bool,
    include_raw: bool,
) -> dict[str, Any]:
    records = list(records)
    meta_record = _find_first_meta(records)
    boundary = (
        _native_boundary(records, meta_record)
        if meta_record is not None
        else None
    )
    events = list(
        _iter_events(
            records,
            meta_record=meta_record,
            boundary=boundary,
            source=source,
            include_inherited=include_inherited,
            include_raw=include_raw,
        )
    )
    _link_tool_events(events)

    meta = _meta_payload(meta_record)
    warnings = [
        f"line {line}: invalid JSON: {error}"
        for line, _entry, error in records
        if error
    ]
    if meta_record is None:
        warnings.append("session_meta not found")
    elif _is_subagent(meta) and boundary is None:
        warnings.append("subagent native boundary not found")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "meta": meta,
        "parent_session_id": _parent_session_id(meta),
        "events": events,
        "warnings": warnings,
    }


def _find_first_meta(
    records: Iterable[tuple[int, Any, str | None]],
) -> tuple[int, Any, str | None] | None:
    for record in records:
        entry = record[1]
        if isinstance(entry, dict) and entry.get("type") == "session_meta":
            return record
    return None


def _scan_session(
    path: str | Path,
) -> tuple[tuple[int, Any, str | None] | None, int | None]:
    meta_record = None
    for record in _read_records(path):
        entry = record[1]
        if meta_record is None:
            if isinstance(entry, dict) and entry.get("type") == "session_meta":
                meta_record = record
            continue
        if _is_native_task_start(entry, meta_record):
            return meta_record, record[0]
    return meta_record, None


def _meta_payload(record: tuple[int, Any, str | None] | None) -> dict[str, Any]:
    if record is None:
        return {}
    payload = record[1].get("payload")
    return payload if isinstance(payload, dict) else {}


def _session_id(meta: dict[str, Any]) -> str:
    return _as_text(meta.get("id") or meta.get("session_id"))


def _parent_session_id(meta: dict[str, Any]) -> str:
    source = meta.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict) and spawn.get("parent_thread_id"):
                return _as_text(spawn["parent_thread_id"])
    return _as_text(meta.get("parent_thread_id") or meta.get("forked_from_id"))


def _is_subagent(meta: dict[str, Any]) -> bool:
    return bool(_parent_session_id(meta))


def _iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _uuid7_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    compact = value.replace("-", "")
    if len(compact) != 32 or compact[12] != "7":
        return None
    try:
        return int(compact[:12], 16) / 1000
    except ValueError:
        return None


def _native_boundary(
    records: Iterable[tuple[int, Any, str | None]],
    meta_record: tuple[int, Any, str | None],
) -> int | None:
    for line, entry, error in records:
        if error or line <= meta_record[0] or not isinstance(entry, dict):
            continue
        if _is_native_task_start(entry, meta_record):
            return line
    return None


def _is_native_task_start(
    entry: Any,
    meta_record: tuple[int, Any, str | None],
) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "event_msg":
        return False
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_started":
        return False

    meta = _meta_payload(meta_record)
    child_uuid_time = _uuid7_timestamp(_session_id(meta))
    turn_time = _uuid7_timestamp(payload.get("turn_id"))
    if child_uuid_time is not None and turn_time is not None:
        return turn_time >= child_uuid_time - _INHERITANCE_TOLERANCE_SECONDS

    child_meta_time = _iso_timestamp(meta.get("timestamp"))
    if child_meta_time is None:
        child_meta_time = _iso_timestamp(meta_record[1].get("timestamp"))
    started_at = payload.get("started_at")
    return (
        child_meta_time is not None
        and isinstance(started_at, (int, float))
        and started_at >= child_meta_time - _INHERITANCE_TOLERANCE_SECONDS
    )


def _is_developer_setup(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("type") != "response_item":
        return False
    payload = entry.get("payload")
    return (
        isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "developer"
    )


def _iter_events(
    records: Iterable[tuple[int, Any, str | None]],
    *,
    meta_record: tuple[int, Any, str | None] | None,
    boundary: int | None,
    source: str,
    include_inherited: bool,
    include_raw: bool,
) -> Iterator[dict[str, Any]]:
    meta = _meta_payload(meta_record)
    session_id = _session_id(meta)
    parent_session_id = _parent_session_id(meta)
    subagent = _is_subagent(meta)

    current_turn = ""
    pending_developer: list[tuple[int, tuple[int, Any, str | None]]] = []
    call_seqs: dict[str, int] = {}

    def emit(
        seq: int,
        record: tuple[int, Any, str | None],
        origin: str,
    ) -> dict[str, Any]:
        nonlocal current_turn
        event = _normalize_record(
            record,
            seq=seq,
            source=source,
            session_id=session_id,
            parent_session_id=parent_session_id,
            current_turn=current_turn,
            origin=origin,
            include_raw=include_raw,
        )
        if event["kind"] == "task_started" and event.get("turn_id"):
            current_turn = event["turn_id"]
        elif event.get("turn_id"):
            current_turn = event["turn_id"]
        call_id = event.get("call_id")
        if event["kind"] == "tool_call" and call_id:
            call_seqs[call_id] = event["seq"]
        elif event["kind"] == "tool_output" and call_id in call_seqs:
            event["paired_seq"] = call_seqs[call_id]
        return event

    for seq, record in enumerate(records, 1):
        line, entry, _error = record
        is_first_meta = meta_record is not None and line == meta_record[0]
        before_boundary = subagent and boundary is not None and line < boundary

        if before_boundary and _is_developer_setup(entry):
            pending_developer.append((seq, record))
            continue

        if pending_developer:
            developer_origin = "native" if line == boundary else "inherited"
            if include_inherited or developer_origin == "native":
                for developer_seq, developer in pending_developer:
                    yield emit(developer_seq, developer, developer_origin)
            pending_developer.clear()

        if not subagent or is_first_meta or (boundary is not None and line >= boundary):
            origin = "native"
        elif boundary is None:
            origin = "unknown"
        else:
            origin = "inherited"
        if include_inherited or origin != "inherited":
            yield emit(seq, record, origin)

    if include_inherited:
        for developer_seq, developer in pending_developer:
            yield emit(developer_seq, developer, "inherited")


def _normalize_record(
    record: tuple[int, Any, str | None],
    *,
    seq: int,
    source: str,
    session_id: str,
    parent_session_id: str,
    current_turn: str,
    origin: str,
    include_raw: bool,
) -> dict[str, Any]:
    line, entry, parse_error = record
    if parse_error:
        return {
            "kind": "parse_error",
            "seq": seq,
            "line": line,
            "timestamp": "",
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "turn_id": current_turn,
            "origin": origin,
            "outer_type": "parse_error",
            "raw_type": "parse_error",
            "error": parse_error,
            "raw": entry,
        }

    if not isinstance(entry, dict):
        outer_type = type(entry).__name__
        payload: dict[str, Any] = {}
        raw_type = outer_type
        kind = "unknown"
        known = False
    else:
        outer_type = _as_text(entry.get("type"))
        raw_payload = entry.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_type = _as_text(payload.get("type")) or outer_type
        kind, known = _event_kind(outer_type, raw_type)

    explicit_turn = payload.get("turn_id")
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not explicit_turn and isinstance(metadata, dict):
        explicit_turn = metadata.get("turn_id")

    event: dict[str, Any] = {
        "kind": kind,
        "seq": seq,
        "line": line,
        "timestamp": _as_text(entry.get("timestamp")) if isinstance(entry, dict) else "",
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "turn_id": _as_text(explicit_turn or current_turn),
        "origin": origin,
        "outer_type": outer_type,
        "raw_type": raw_type,
    }
    _add_derived_fields(event, payload)
    if include_raw or not known:
        event["raw"] = entry
    return event


def _event_kind(outer_type: str, raw_type: str) -> tuple[str, bool]:
    if outer_type == "event_msg":
        return (raw_type, True) if raw_type in _EVENT_KINDS else ("unknown", False)
    if outer_type == "response_item":
        return (_RESPONSE_KINDS[raw_type], True) if raw_type in _RESPONSE_KINDS else ("unknown", False)
    if outer_type in _OUTER_KINDS:
        return outer_type, True
    return "unknown", False


def _add_derived_fields(event: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in (
        "arguments",
        "author",
        "call_id",
        "content",
        "full",
        "id",
        "info",
        "input",
        "local_images",
        "model_context_window",
        "name",
        "namespace",
        "num_turns",
        "output",
        "phase",
        "rate_limits",
        "reason",
        "recipient",
        "role",
        "status",
        "stderr",
        "stdout",
        "summary",
        "success",
        "thread_settings",
    ):
        if key in payload:
            event[key] = payload[key]

    text = payload.get("message", payload.get("text", payload.get("last_agent_message")))
    if text is None and isinstance(payload.get("content"), list):
        parts = [
            block.get("text", "")
            for block in payload["content"]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        text = "\n".join(part for part in parts if part)
    if text is None and isinstance(payload.get("summary"), list):
        parts = [
            item.get("text", "")
            for item in payload["summary"]
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        text = "\n".join(part for part in parts if part)
    if text is not None:
        event["text"] = _as_text(text)
    if event["kind"] == "tool_call" and not event.get("name"):
        event["name"] = event["raw_type"]


def _link_tool_events(events: list[dict[str, Any]]) -> None:
    calls: dict[str, dict[str, Any]] = {}
    pending_outputs: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        call_id = event.get("call_id")
        if not call_id:
            continue
        if event["kind"] == "tool_call":
            calls[call_id] = event
            for output in pending_outputs.pop(call_id, []):
                event["paired_seq"] = output["seq"]
                output["paired_seq"] = event["seq"]
        elif event["kind"] == "tool_output" and call_id in calls:
            event["paired_seq"] = calls[call_id]["seq"]
            calls[call_id]["paired_seq"] = event["seq"]
        elif event["kind"] == "tool_output":
            pending_outputs.setdefault(call_id, []).append(event)


def _has_positive_usage(total: dict[str, Any]) -> bool:
    """Return True when total token usage contains any positive numeric value."""
    return any(
        isinstance(value, (int, float)) and value > 0
        for value in total.values()
    )


def extract_conversation(
    entries: list[dict],
) -> tuple[dict | None, list[dict]]:
    """Extract session metadata and meaningful conversation events.

    Returns (meta, events) where meta is the session_meta payload and events
    is a flat list of typed dicts representing user messages, assistant
    responses, tool calls, reasoning blocks, and system events.
    """
    raw_events: list[dict] = []
    meta: dict | None = None
    turn_seq = 0

    for entry in entries:
        ts = entry.get("timestamp", "")
        etype = entry.get("type", "")
        payload = entry.get("payload") or {}

        if etype == "session_meta":
            meta = payload
            continue

        if etype == "event_msg":
            if payload.get("type", "") == "task_started":
                turn_seq += 1
            _handle_event_msg(payload, ts, raw_events, turn_seq)
            continue

        if etype == "response_item":
            _handle_response_item(payload, ts, raw_events, turn_seq)
            continue

    reconciled = _reconcile_events(raw_events)
    cleaned = [_strip_internal_keys(event) for event in reconciled]
    return meta, cleaned


def _handle_event_msg(
    payload: dict[str, Any],
    ts: str,
    events: list[dict],
    turn_seq: int,
) -> None:
    msg_type = payload.get("type", "")

    if msg_type == "user_message":
        events.append(
            {
                "type": "user_message",
                "ts": ts,
                "text": _as_text(payload.get("message", "")),
                "images": payload.get("local_images", []),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "agent_message":
        events.append(
            {
                "type": "agent_commentary",
                "ts": ts,
                "text": _as_text(payload.get("message", "")),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "agent_reasoning":
        events.append(
            {
                "type": "reasoning",
                "ts": ts,
                "text": _as_text(payload.get("text", "")),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "task_complete":
        events.append(
            {
                "type": "task_complete",
                "ts": ts,
                "text": _as_text(payload.get("last_agent_message", "")),
                "turn_id": _as_text(payload.get("turn_id", "")),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "task_started":
        events.append(
            {
                "type": "task_started",
                "ts": ts,
                "turn_id": _as_text(payload.get("turn_id", "")),
                "model_context_window": payload.get("model_context_window", ""),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "turn_aborted":
        events.append(
            {
                "type": "turn_aborted",
                "ts": ts,
                "reason": _as_text(payload.get("reason", "")),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type == "token_count":
        info = payload.get("info") or {}
        total = info.get("total_token_usage", {})
        if isinstance(total, dict) and total and _has_positive_usage(total):
            rate_limits = payload.get("rate_limits")
            limit_id = (
                _as_text(rate_limits.get("limit_id", ""))
                if isinstance(rate_limits, dict)
                else ""
            )
            events.append(
                {
                    "type": "token_count",
                    "ts": ts,
                    "total": total,
                    "rate_limit_ids": [limit_id] if limit_id else [],
                    "rate_limits": [rate_limits] if isinstance(rate_limits, dict) else [],
                    "_source": "event_msg",
                    "_turn_seq": turn_seq,
                }
            )
    elif msg_type == "thread_rolled_back":
        events.append(
            {
                "type": "thread_rolled_back",
                "ts": ts,
                "num_turns": payload.get("num_turns", 0),
                "_source": "event_msg",
                "_turn_seq": turn_seq,
            }
        )
    elif msg_type in {
        "patch_apply_begin",
        "patch_apply_end",
        "sub_agent_activity",
        "thread_settings_applied",
        "web_search_begin",
        "web_search_end",
    }:
        event = {
            "type": msg_type,
            "ts": ts,
            "_source": "event_msg",
            "_turn_seq": turn_seq,
        }
        event.update({key: value for key, value in payload.items() if key != "type"})
        events.append(event)


def _handle_response_item(
    payload: dict[str, Any],
    ts: str,
    events: list[dict],
    turn_seq: int,
) -> None:
    item_type = payload.get("type", "")
    role = payload.get("role", "")

    if item_type in {"function_call", "custom_tool_call"}:
        events.append(
            {
                "type": "tool_call",
                "ts": ts,
                "name": _as_text(payload.get("name", "")),
                "arguments": _as_text(
                    payload.get("arguments", payload.get("input", ""))
                ),
                "call_id": _as_text(payload.get("call_id", "")),
                "_source": "response_item",
                "_turn_seq": turn_seq,
            }
        )
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        events.append(
            {
                "type": "tool_output",
                "ts": ts,
                "call_id": _as_text(payload.get("call_id", "")),
                "output": _as_text(payload.get("output", "")),
                "_source": "response_item",
                "_turn_seq": turn_seq,
            }
        )
    elif item_type == "agent_message":
        content = payload.get("content", [])
        text = "\n".join(
            _as_text(block.get("text"))
            for block in content
            if isinstance(block, dict) and block.get("type") == "input_text"
        )
        events.append(
            {
                "type": "inter_agent_message",
                "ts": ts,
                "text": text,
                "author": _as_text(payload.get("author")),
                "recipient": _as_text(payload.get("recipient")),
                "_source": "response_item",
                "_turn_seq": turn_seq,
            }
        )
    elif item_type == "message" and role in {"assistant", "user"}:
        content = payload.get("content", [])
        phase = payload.get("phase", "")
        for block in content:
            block_type = block.get("type")
            if role == "assistant" and block_type == "output_text":
                events.append(
                    {
                        "type": "assistant_text",
                        "ts": ts,
                        "text": _as_text(block.get("text", "")),
                        "phase": _as_text(phase),
                        "_source": "response_item",
                        "_turn_seq": turn_seq,
                    }
                )
            elif role == "user" and block_type == "input_text":
                events.append(
                    {
                        "type": "user_message",
                        "ts": ts,
                        "text": _as_text(block.get("text", "")),
                        "images": [],
                        "_source": "response_item",
                        "_turn_seq": turn_seq,
                    }
                )
    elif item_type == "reasoning":
        summary = payload.get("summary", [])
        for s in summary:
            if s.get("type") == "summary_text":
                events.append(
                    {
                        "type": "reasoning",
                        "ts": ts,
                        "text": _as_text(s.get("text", "")),
                        "_source": "response_item",
                        "_turn_seq": turn_seq,
                    }
                )


def _project_normalized_event(event: dict[str, Any], turn_seq: int) -> dict | None:
    kind = event.get("kind")
    if kind == "session_meta":
        return None

    outer_type = event.get("outer_type")
    raw_type = event.get("raw_type")
    role = event.get("role")
    projected_type = kind
    if outer_type == "event_msg" and kind == "agent_message":
        projected_type = "agent_commentary"
    elif outer_type == "event_msg" and kind == "agent_reasoning":
        projected_type = "reasoning"
    elif outer_type == "response_item" and raw_type == "message":
        if role == "assistant":
            projected_type = "assistant_text"
        elif role == "user":
            projected_type = "user_message"
        else:
            return None
    elif outer_type == "response_item" and raw_type == "agent_message":
        projected_type = "inter_agent_message"

    projected: dict[str, Any] = {
        "type": projected_type,
        "ts": event.get("timestamp", ""),
        "_source": event.get("outer_type", "normalized"),
        "_turn_seq": turn_seq,
    }
    for key in (
        "arguments",
        "author",
        "call_id",
        "content",
        "error",
        "input",
        "name",
        "namespace",
        "output",
        "parent_session_id",
        "phase",
        "recipient",
        "role",
        "session_id",
        "status",
        "summary",
        "text",
        "turn_id",
    ):
        if key in event:
            projected[key] = event[key]

    if kind == "parse_error":
        projected["text"] = _as_text(event.get("raw"))
    elif kind == "unknown":
        projected["raw_type"] = event.get("raw_type", "")
        projected["detail"] = json.dumps(event.get("raw"), ensure_ascii=False, indent=2)
    elif kind == "world_state":
        raw = event.get("raw")
        payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
        projected["full"] = payload.get("full", event.get("full", False))
    elif kind == "token_count":
        info = event.get("info") or {}
        projected["total"] = info.get("total_token_usage", {})
        rate_limits = event.get("rate_limits")
        projected["rate_limits"] = [rate_limits] if isinstance(rate_limits, dict) else []
    elif kind == "user_message":
        projected["images"] = event.get("local_images", [])
    for key in (
        "model_context_window",
        "num_turns",
        "reason",
        "stderr",
        "stdout",
        "success",
        "thread_settings",
    ):
        if key in event:
            projected[key] = event[key]
    return projected


def _merge_adjacent_token_events(events: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for event in events:
        if (
            event.get("type") == "token_count"
            and merged
            and merged[-1].get("type") == "token_count"
            and merged[-1].get("_turn_seq") == event.get("_turn_seq")
            and merged[-1].get("total") == event.get("total")
        ):
            _merge_token_metadata(merged[-1], event)
            continue
        merged.append(event.copy())
    return merged


def _merge_token_metadata(base_event: dict, event: dict) -> None:
    base_ids = list(base_event.get("rate_limit_ids", []))
    seen_ids = set(base_ids)
    for limit_id in event.get("rate_limit_ids", []):
        if limit_id not in seen_ids:
            base_ids.append(limit_id)
            seen_ids.add(limit_id)
    base_event["rate_limit_ids"] = base_ids

    base_rate_limits = list(base_event.get("rate_limits", []))
    for rate_limit in event.get("rate_limits", []):
        if isinstance(rate_limit, dict) and rate_limit not in base_rate_limits:
            base_rate_limits.append(rate_limit)
    base_event["rate_limits"] = base_rate_limits


def _normalize_text(value: Any) -> str:
    return " ".join(_as_text(value).split())


def _is_response_counterpart(candidate: dict, response_event: dict) -> bool:
    if response_event.get("_source") != "response_item":
        return False
    if candidate.get("_turn_seq") != response_event.get("_turn_seq"):
        return False

    candidate_type = candidate.get("type")
    if candidate_type == "user_message":
        if response_event.get("type") != "user_message":
            return False
    elif candidate_type == "agent_commentary":
        if response_event.get("type") != "assistant_text":
            return False
        if response_event.get("phase") == "final_answer":
            return False
    elif candidate_type == "reasoning":
        if response_event.get("type") != "reasoning":
            return False
    elif candidate_type == "task_complete":
        if response_event.get("type") != "assistant_text":
            return False
        if response_event.get("phase") != "final_answer":
            return False
    else:
        return False

    return _as_text(candidate.get("text")) == _as_text(response_event.get("text"))


def _find_matching_response_index(
    events: list[dict],
    idx: int,
    used_indices: set[int],
    *,
    window: int = 8,
) -> int | None:
    candidate = events[idx]
    if candidate.get("_source") != "event_msg":
        return None

    candidate_type = candidate.get("type")
    if candidate_type not in {
        "agent_commentary",
        "reasoning",
        "task_complete",
        "user_message",
    }:
        return None

    start = max(0, idx - window)
    end = min(len(events), idx + window + 1)
    for j in range(start, end):
        if j == idx or j in used_indices:
            continue
        if _is_response_counterpart(candidate, events[j]):
            return j
    return None


def _drop_overlapped_event_msg_events(events: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    used_response_indices: set[int] = set()

    for idx, event in enumerate(events):
        if event.get("type") == "task_complete" and not _normalize_text(event.get("text", "")):
            continue

        match_idx = _find_matching_response_index(events, idx, used_response_indices)
        if match_idx is not None:
            used_response_indices.add(match_idx)
            continue

        filtered.append(event)

    return filtered


def _reconcile_events(events: list[dict]) -> list[dict]:
    merged = _merge_adjacent_token_events(events)
    return _drop_overlapped_event_msg_events(merged)


def _strip_internal_keys(event: dict) -> dict:
    return {k: v for k, v in event.items() if not k.startswith("_")}
