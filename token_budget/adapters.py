"""Read-only adapters for Codex and Claude JSONL session logs."""
from __future__ import annotations
import json
from pathlib import Path
from .core import UsageEvent

MAX_INT = (1 << 63) - 1

def _int(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > MAX_INT:
        return None
    return value


def _usage_value(usage: dict, key: str, required: bool = False) -> int | None:
    if key not in usage:
        return None if required else 0
    return _int(usage[key])


def _metadata_string(value: object) -> str | None | object:
    """Return nullable scalar metadata, or a sentinel-invalid object."""
    if value is None:
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return _metadata_string.invalid
        return value
    return _metadata_string.invalid


_metadata_string.invalid = object()


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("**/*.jsonl"), key=lambda path: path.relative_to(root).as_posix())


def codex_events(root: str | Path) -> list[UsageEvent]:
    latest_events: dict[str, UsageEvent] = {}
    root_path = Path(root)
    for path in _files(root_path):
        try: lines = path.read_text(errors="replace").splitlines()
        except OSError: continue
        for line_no, line in enumerate(lines):
            try: record = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError): continue
            if not isinstance(record, dict): continue
            payload = record.get("payload")
            if not isinstance(payload, dict): continue
            if payload.get("type") != "token_count": continue
            info = payload.get("info")
            if not isinstance(info, dict): continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict): continue
            timestamp = _metadata_string(record.get("timestamp"))
            explicit_session = payload.get("session_id") or record.get("session_id")
            session_metadata = _metadata_string(explicit_session)
            model = _metadata_string(record.get("model", payload.get("model")))
            if timestamp is _metadata_string.invalid or session_metadata is _metadata_string.invalid or model is _metadata_string.invalid: continue
            raw_ordinal = record.get("ordinal")
            if raw_ordinal is not None and (type(raw_ordinal) is not int or raw_ordinal < 0 or raw_ordinal > MAX_INT): continue
            input_tokens = _usage_value(usage, "input_tokens", True)
            output_tokens = _usage_value(usage, "output_tokens", True)
            total_tokens = _usage_value(usage, "total_tokens", True)
            optional = [_usage_value(usage, key) for key in ("cached_input_tokens", "reasoning_output_tokens", "cache_write_input_tokens")]
            if input_tokens is None or output_tokens is None or total_tokens is None or any(value is None for value in optional): continue
            cumulative = None
            if raw_ordinal is None:
                cumulative_usage = info.get("total_token_usage")
                if not isinstance(cumulative_usage, dict): continue
                primary = [_usage_value(cumulative_usage, key, True) for key in
                           ("input_tokens", "output_tokens", "total_tokens")]
                auxiliary = [_usage_value(cumulative_usage, key) for key in
                             ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")]
                if any(value is None for value in primary) or any(value is None for value in auxiliary): continue
                cumulative = [primary[0], auxiliary[0], auxiliary[1], primary[1], auxiliary[2], primary[2]]
            relative = path.relative_to(root_path) if path.is_relative_to(root_path) else path
            session_id = session_metadata or path.stem
            event_key = (f"session:{session_id}:ordinal:{raw_ordinal}" if raw_ordinal is not None
                         else f"session:{session_id}:cumulative:{','.join(str(value) for value in cumulative)}")
            if _metadata_string(session_id) is _metadata_string.invalid or _metadata_string(event_key) is _metadata_string.invalid: continue
            revision_key = f"{relative}\x00{line_no:020d}"
            if _metadata_string(revision_key) is _metadata_string.invalid: continue
            event = UsageEvent(source="codex", occurred_at=timestamp,
                session_id=session_id,
                model=model, ordinal=raw_ordinal,
                event_key=event_key, input_tokens=input_tokens,
                output_tokens=output_tokens, cached_input_tokens=optional[0],
                cache_creation_input_tokens=optional[2], reasoning_tokens=optional[1], total_tokens=total_tokens,
                revision=raw_ordinal if raw_ordinal is not None else line_no, revision_key=revision_key)
            latest_events[event_key] = event
    return list(latest_events.values())


def claude_events(root: str | Path) -> list[UsageEvent]:
    root_path = Path(root)
    latest: dict[tuple[str, str, str], UsageEvent] = {}
    for path in _files(root_path):
        try: lines = path.read_text(errors="replace").splitlines()
        except OSError: continue
        for line_no, line in enumerate(lines):
            try: record = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError): continue
            if not isinstance(record, dict) or record.get("type") != "assistant": continue
            message = record.get("message")
            if not isinstance(message, dict): continue
            timestamp = _metadata_string(record.get("timestamp"))
            session_metadata = _metadata_string(record.get("sessionId"))
            model = _metadata_string(message.get("model"))
            request_metadata = _metadata_string(record.get("requestId"))
            if timestamp is _metadata_string.invalid or session_metadata is _metadata_string.invalid or model is _metadata_string.invalid or request_metadata is _metadata_string.invalid: continue
            usage = message.get("usage")
            if not isinstance(usage, dict): continue
            input_tokens = _usage_value(usage, "input_tokens", True)
            output_tokens = _usage_value(usage, "output_tokens", True)
            optional = [_usage_value(usage, key) for key in ("cache_read_input_tokens", "cache_creation_input_tokens")]
            if input_tokens is None or output_tokens is None or any(value is None for value in optional): continue
            total_tokens = input_tokens + optional[0] + optional[1] + output_tokens
            if total_tokens > MAX_INT: continue
            session_id = session_metadata or path.stem
            request_id = request_metadata
            relative = path.relative_to(root_path) if path.is_relative_to(root_path) else path
            revision_key = f"{relative}\x00{line_no:020d}"
            if _metadata_string(revision_key) is _metadata_string.invalid: continue
            if request_id:
                # Claude transcripts repeat a request on multiple lines. Keep the
                # last valid occurrence for each session and request ID.
                grouping_key = (str(session_id), "request", str(request_id))
                event_key = f"session:{session_id}:request:{request_id}"
            else:
                # A line-specific fallback prevents unrelated requests from
                # merging when older transcripts omit requestId.
                grouping_key = (str(session_id), "line", f"{relative}:{line_no}")
                event_key = f"{relative}:{line_no}"
            if _metadata_string(event_key) is _metadata_string.invalid: continue
            latest[grouping_key] = UsageEvent(source="claude", occurred_at=timestamp,
                session_id=session_id, model=model, ordinal=line_no, event_key=event_key, input_tokens=input_tokens,
                output_tokens=output_tokens, cached_input_tokens=optional[0],
                cache_creation_input_tokens=optional[1], reasoning_tokens=0,
                total_tokens=total_tokens, request_id=str(request_id) if request_id else None,
                revision=line_no, revision_key=revision_key)
    return list(latest.values())
