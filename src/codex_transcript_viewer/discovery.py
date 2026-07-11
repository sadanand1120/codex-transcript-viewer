"""Discover Codex session files and parent/subagent relationships."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def session_files(sessions_dir: str | Path | None = None) -> list[Path]:
    root = Path(sessions_dir) if sessions_dir else default_sessions_dir()
    return list(root.rglob("*.jsonl")) if root.is_dir() else []


def read_session_meta(path: str | Path) -> dict[str, Any]:
    """Read only the first session_meta record from a transcript."""
    with Path(path).open(encoding="utf-8") as transcript:
        for line in transcript:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session_meta":
                payload = entry.get("payload")
                return payload if isinstance(payload, dict) else {}
    return {}


def session_summary(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    meta = read_session_meta(path)
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else {}
    spawn = spawn if isinstance(spawn, dict) else {}
    return {
        "id": str(meta.get("id") or meta.get("session_id") or ""),
        "timestamp": str(meta.get("timestamp") or ""),
        "thread_source": str(meta.get("thread_source") or ""),
        "parent_id": str(spawn.get("parent_thread_id") or ""),
        "agent_path": str(spawn.get("agent_path") or ""),
        "agent_nickname": str(spawn.get("agent_nickname") or ""),
        "cwd": str(meta.get("cwd") or ""),
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }


def list_sessions(
    sessions_dir: str | Path | None = None,
    *,
    limit: int = 20,
    cwd: str | None = None,
    thread_source: str | None = None,
) -> list[dict[str, Any]]:
    summaries = [session_summary(path) for path in session_files(sessions_dir)]
    if cwd:
        summaries = [item for item in summaries if item["cwd"] == cwd]
    if thread_source:
        summaries = [item for item in summaries if item["thread_source"] == thread_source]
    summaries.sort(key=lambda item: (item["timestamp"], item["mtime"]), reverse=True)
    return summaries[:limit]


def resolve_session(reference: str, sessions_dir: str | Path | None = None) -> Path:
    candidate = Path(reference).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    summaries = [session_summary(path) for path in session_files(sessions_dir)]
    matches = [item for item in summaries if item["id"].startswith(reference)]
    if not matches:
        raise FileNotFoundError(f"session not found: {reference}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous session prefix {reference!r}: {len(matches)} matches")
    return Path(matches[0]["path"])


def build_tree(reference: str, sessions_dir: str | Path | None = None) -> dict[str, Any]:
    target = resolve_session(reference, sessions_dir)
    summaries = [session_summary(path) for path in session_files(sessions_dir)]
    by_id = {item["id"]: item for item in summaries if item["id"]}
    target_id = session_summary(target)["id"]
    root_id = target_id
    seen: set[str] = set()
    while root_id in by_id and by_id[root_id]["parent_id"] and root_id not in seen:
        seen.add(root_id)
        root_id = by_id[root_id]["parent_id"]

    children: dict[str, list[str]] = {}
    for item in summaries:
        if item["parent_id"]:
            children.setdefault(item["parent_id"], []).append(item["id"])

    nodes: list[dict[str, Any]] = []

    def visit(session_id: str, depth: int) -> None:
        item = by_id.get(session_id)
        if not item:
            return
        node = {key: value for key, value in item.items() if key != "mtime"}
        node["depth"] = depth
        node["selected"] = session_id == target_id
        nodes.append(node)
        for child_id in sorted(children.get(session_id, [])):
            visit(child_id, depth + 1)

    visit(root_id, 0)
    return {"root_id": root_id, "selected_id": target_id, "nodes": nodes}
