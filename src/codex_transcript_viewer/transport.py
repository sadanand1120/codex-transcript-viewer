"""Safely stage Codex sessions from configured SSH hosts."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .discovery import (
    build_tree_from_summaries,
    resolve_session,
    session_summary_from_meta,
)


_HOST = re.compile(r"[A-Za-z0-9_.@-]+")
_SESSION = re.compile(r"(?=.*[0-9A-Fa-f])[0-9A-Fa-f-]{4,36}")

_REMOTE_PROGRAM = r'''import base64
import json
import os
import shutil
import sys
from pathlib import Path

request = json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode())
configured = request.get("sessions_dir")
root = (
    Path(configured).expanduser()
    if configured
    else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
)

if not root.is_dir():
    raise SystemExit(f"remote sessions directory not found: {root}")

def read_meta(path):
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict):
                    meta = entry["payload"]
                    keys = (
                        "id", "session_id", "timestamp", "thread_source", "source",
                        "cwd", "parent_thread_id", "forked_from_id",
                    )
                    return {key: meta[key] for key in keys if key in meta}
    except OSError:
        pass
    return None

sessions = []
for path in sorted(root.rglob("*.jsonl")):
    meta = read_meta(path)
    if meta:
        try:
            stat = path.stat()
        except OSError:
            continue
        sessions.append({
            "path": str(path), "bytes": stat.st_size,
            "mtime": stat.st_mtime, "meta": meta,
        })

reference = request["session"]
matches = [
    item for item in sessions
    if str(item["meta"].get("id") or item["meta"].get("session_id") or "")
    .startswith(reference)
]
if not matches:
    raise SystemExit(f"remote session not found: {reference}")
if len(matches) != 1:
    raise SystemExit(f"ambiguous remote session prefix {reference!r}: {len(matches)} matches")
selected = matches[0]

if request["action"] == "fetch":
    with open(selected["path"], "rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)
elif request["action"] == "manifest":
    print(json.dumps({
        "selected_id": selected["meta"].get("id") or selected["meta"].get("session_id"),
        "sessions": sessions,
    }, ensure_ascii=False))
else:
    raise SystemExit("unsupported remote action")
'''


@dataclass(frozen=True)
class RemoteReference:
    host: str
    session: str

    @property
    def display(self) -> str:
        return f"{self.host}:{self.session}"


@dataclass(frozen=True)
class SessionSource:
    path: Path
    remote: RemoteReference | None = None


def parse_remote_reference(value: str) -> RemoteReference | None:
    if ":" not in value:
        return None
    host, session = value.split(":", 1)
    if not _HOST.fullmatch(host) or host.startswith("-"):
        raise ValueError(f"invalid remote host in session reference: {host!r}")
    if not _SESSION.fullmatch(session):
        raise ValueError("remote session must be an ID or unique ID prefix")
    return RemoteReference(host, session)


def _remote_script(request: dict) -> bytes:
    encoded = base64.urlsafe_b64encode(
        json.dumps(request, separators=(",", ":")).encode()
    ).decode()
    return (
        f"request='{encoded}'\n"
        "python3 - \"$request\" <<'PY'\n"
        f"{_REMOTE_PROGRAM}\n"
        "PY\n"
    ).encode()


def _run_ssh_script(
    remote: RemoteReference,
    action: str,
    sessions_dir: str | None,
    output: BinaryIO | None = None,
) -> bytes:
    executable = shutil.which("ssh-script")
    if not executable:
        raise FileNotFoundError("ssh-script is not installed or not on PATH")
    request = {
        "action": action,
        "session": remote.session,
        "sessions_dir": sessions_dir or "",
    }
    result = subprocess.run(
        [executable, remote.host],
        input=_remote_script(request),
        stdout=output if output is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip()[-2000:]
        raise RuntimeError(message or f"ssh-script failed for {remote.host}")
    return result.stdout or b""


def _open_private_binary(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    os.ftruncate(descriptor, 0)
    return os.fdopen(descriptor, "wb")


@contextmanager
def open_session_source(
    reference: str,
    sessions_dir: str | Path | None = None,
) -> Iterator[SessionSource]:
    candidate = Path(reference).expanduser()
    if candidate.is_file():
        yield SessionSource(candidate.resolve())
        return

    remote = parse_remote_reference(reference)
    if remote is None:
        yield SessionSource(resolve_session(reference, sessions_dir))
        return

    with tempfile.TemporaryDirectory(prefix="codex-transcript-remote-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        path = root / "session.jsonl"
        with _open_private_binary(path) as output:
            _run_ssh_script(remote, "fetch", str(sessions_dir) if sessions_dir else None, output)
        if not path.stat().st_size:
            raise RuntimeError(f"remote session was empty: {remote.display}")
        yield SessionSource(path, remote)


def build_remote_tree(
    remote: RemoteReference,
    sessions_dir: str | Path | None = None,
) -> dict:
    payload = _run_ssh_script(
        remote,
        "manifest",
        str(sessions_dir) if sessions_dir else None,
    )
    try:
        manifest = json.loads(payload)
        summaries = [
            session_summary_from_meta(
                item["meta"],
                item["path"],
                size=item["bytes"],
                mtime=item["mtime"],
            )
            for item in manifest["sessions"]
        ]
        tree = build_tree_from_summaries(manifest["selected_id"], summaries)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid remote session manifest") from error

    for node in tree["nodes"]:
        node["host"] = remote.host
        node["path"] = f"{remote.host}:{node['path']}"
    return tree
