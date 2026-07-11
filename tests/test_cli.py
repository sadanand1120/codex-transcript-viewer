from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from codex_transcript_viewer import cli
from codex_transcript_viewer.discovery import build_tree, resolve_session


ROOT_ID = "019f6000-0000-7000-8000-000000000001"
CHILD_ID = "019f6000-1000-7000-8000-000000000002"
TURN_ID = "019f6000-2000-7000-8000-000000000003"


def write_session(path: Path, session_id: str, *, parent_id: str = "") -> None:
    source = "cli"
    thread_source = "user"
    if parent_id:
        thread_source = "subagent"
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                    "agent_path": "/root/tester",
                    "agent_nickname": "Tester",
                }
            }
        }
    records = [
        {
            "timestamp": "2026-07-11T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "timestamp": "2026-07-11T12:00:00Z",
                "thread_source": thread_source,
                "source": source,
                "cwd": "/repo",
            },
        },
        {
            "timestamp": "2026-07-11T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_ID, "started_at": 1783771201},
        },
        {
            "timestamp": "2026-07-11T12:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect parser"},
        },
        {
            "timestamp": "2026-07-11T12:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-1",
                "input": "{\"cmd\":\"rg parser\"}",
            },
        },
        {
            "timestamp": "2026-07-11T12:00:04Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "call-1", "output": "ok"},
        },
        {
            "timestamp": "2026-07-11T12:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_path = self.root / "2026" / "07" / "11" / "root.jsonl"
        self.child_path = self.root / "2026" / "07" / "11" / "child.jsonl"
        write_session(self.root_path, ROOT_ID)
        write_session(self.child_path, CHILD_ID, parent_id=ROOT_ID)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resolve_latest_prefers_root_and_tree_links_child(self) -> None:
        self.assertEqual(resolve_session("latest", self.root), self.root_path.resolve())
        tree = build_tree(CHILD_ID[:12], self.root)
        self.assertEqual(tree["root_id"], ROOT_ID)
        self.assertEqual([node["id"] for node in tree["nodes"]], [ROOT_ID, CHILD_ID])
        self.assertTrue(tree["nodes"][1]["selected"])

    def test_query_compact_jsonl(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.main([
                "--sessions-dir", str(self.root), "query", str(self.root_path),
                "--kind", "tool_call", "--compact", "--format", "jsonl",
            ])
        event = json.loads(stdout.getvalue())
        self.assertEqual(event["kind"], "tool_call")
        self.assertEqual(event["name"], "exec")
        self.assertNotIn("raw", event)

    def test_render_is_private_and_marks_final_answer(self) -> None:
        output = self.root / "viewer.html"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.main(["--json", "render", str(self.root_path), "--output", str(output)])
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        html = output.read_text(encoding="utf-8")
        self.assertIn("final-answer", html)
        self.assertIn("rg parser", html)

    def test_redaction_preserves_token_usage_metrics(self) -> None:
        value = {
            "total_token_usage": {"input_tokens": 12},
            "access_token": "secret",
            "arguments": 'curl -H "Authorization: Bearer sk-live-SECRET"',
            "output": "OPENAI_API_KEY=sk-live-SECRET",
        }
        redacted = cli._redact(value)
        self.assertEqual(redacted["total_token_usage"]["input_tokens"], 12)
        self.assertEqual(redacted["access_token"], "<redacted>")
        self.assertNotIn("sk-live-SECRET", redacted["arguments"])
        self.assertNotIn("sk-live-SECRET", redacted["output"])

    def test_private_writer_tightens_existing_permissions(self) -> None:
        output = self.root / "existing.txt"
        output.write_text("public", encoding="utf-8")
        output.chmod(0o644)
        cli._write_private(output, "private")
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        self.assertEqual(output.read_text(encoding="utf-8"), "private")

    def test_browser_uses_private_deterministic_temp_file(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(cli.tempfile, "gettempdir", return_value=str(self.root)),
            mock.patch.object(cli.webbrowser, "open", return_value=True) as opener,
            redirect_stdout(stdout),
        ):
            cli.main(["--json", "browser", str(self.root_path)])
        result = json.loads(stdout.getvalue())["data"]
        output = Path(result["path"])
        self.assertEqual(output.name, f"{ROOT_ID}.html")
        self.assertEqual(os.stat(output.parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        opener.assert_called_once_with(output.as_uri())

    def test_browser_sanitizes_transcript_controlled_session_id(self) -> None:
        write_session(self.root_path, "/home/user/report")
        stdout = io.StringIO()
        with (
            mock.patch.object(cli.tempfile, "gettempdir", return_value=str(self.root)),
            mock.patch.object(cli.webbrowser, "open", return_value=True),
            redirect_stdout(stdout),
        ):
            cli.main(["--json", "browser", str(self.root_path)])
        output = Path(json.loads(stdout.getvalue())["data"]["path"])
        self.assertEqual(output.parent, self.root / "codex-transcript")
        self.assertEqual(output.name, "_home_user_report.html")

    def test_non_positive_limit_is_rejected(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["list", "--limit", "0"])


if __name__ == "__main__":
    unittest.main()
