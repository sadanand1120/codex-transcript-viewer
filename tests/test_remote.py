from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_transcript_viewer import cli, transport
from codex_transcript_viewer.discovery import read_session_meta
from codex_transcript_viewer.transport import RemoteReference, SessionSource
from test_cli import CHILD_ID, ROOT_ID, write_session


class RemoteTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_path = self.root / "root.jsonl"
        self.child_path = self.root / "child.jsonl"
        write_session(self.root_path, ROOT_ID)
        write_session(self.child_path, CHILD_ID, parent_id=ROOT_ID)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reference_parser_is_strict(self) -> None:
        remote = transport.parse_remote_reference(f"robolang:{ROOT_ID[:12]}")
        self.assertEqual(remote, RemoteReference("robolang", ROOT_ID[:12]))
        self.assertIsNone(transport.parse_remote_reference(ROOT_ID))
        for value in ("-oProxyCommand=x:019f", "robo lang:019f", "robolang:../../etc/passwd"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                transport.parse_remote_reference(value)

    def test_ssh_invocation_uses_wrapper_argv_and_encoded_stdin(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=b"payload", stderr=b"")
        remote = RemoteReference("robolang", ROOT_ID)
        with (
            mock.patch.object(transport.shutil, "which", return_value="/usr/bin/ssh-script"),
            mock.patch.object(transport.subprocess, "run", return_value=completed) as runner,
        ):
            result = transport._run_ssh_script(remote, "fetch", None)

        self.assertEqual(result, b"payload")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ["/usr/bin/ssh-script", "robolang"])
        self.assertNotIn(ROOT_ID.encode(), kwargs["input"])
        self.assertFalse(kwargs.get("shell", False))

    def test_remote_source_is_private_and_cleaned(self) -> None:
        staged_path = None

        def fake_run(_remote, action, _sessions_dir, output=None):
            self.assertEqual(action, "fetch")
            output.write(self.root_path.read_bytes())
            return b""

        with mock.patch.object(transport, "_run_ssh_script", side_effect=fake_run):
            with transport.open_session_source(f"robolang:{ROOT_ID}") as source:
                staged_path = source.path
                self.assertEqual(os.stat(source.path.parent).st_mode & 0o777, 0o700)
                self.assertEqual(os.stat(source.path).st_mode & 0o777, 0o600)
                self.assertEqual(read_session_meta(source.path)["id"], ROOT_ID)
        self.assertFalse(staged_path.exists())

    def test_remote_source_is_cleaned_after_fetch_failure(self) -> None:
        staging_root = self.root / "staging"
        staging_root.mkdir()
        with (
            mock.patch.object(transport.tempfile, "tempdir", str(staging_root)),
            mock.patch.object(transport, "_run_ssh_script", side_effect=RuntimeError("ssh failed")),
            self.assertRaises(RuntimeError),
        ):
            with transport.open_session_source(f"robolang:{ROOT_ID}"):
                pass
        self.assertEqual(list(staging_root.iterdir()), [])

    def test_remote_tree_uses_host_qualified_paths(self) -> None:
        manifest = {
            "selected_id": CHILD_ID,
            "sessions": [
                {
                    "path": "/home/test/.codex/sessions/root.jsonl",
                    "bytes": self.root_path.stat().st_size,
                    "mtime": 1,
                    "meta": read_session_meta(self.root_path),
                },
                {
                    "path": "/home/test/.codex/sessions/child.jsonl",
                    "bytes": self.child_path.stat().st_size,
                    "mtime": 2,
                    "meta": read_session_meta(self.child_path),
                },
            ],
        }
        with mock.patch.object(
            transport,
            "_run_ssh_script",
            return_value=json.dumps(manifest).encode(),
        ):
            tree = transport.build_remote_tree(RemoteReference("robolang", CHILD_ID[:12]))

        self.assertEqual([node["id"] for node in tree["nodes"]], [ROOT_ID, CHILD_ID])
        self.assertTrue(tree["nodes"][1]["selected"])
        self.assertTrue(all(node["path"].startswith("robolang:/home/test/") for node in tree["nodes"]))

    def test_all_session_commands_accept_remote_reference(self) -> None:
        remote = RemoteReference("robolang", ROOT_ID)

        @contextmanager
        def fake_source(_reference, _sessions_dir):
            yield SessionSource(self.root_path, remote)

        render_output = self.root / "render.html"
        export_output = self.root / "export.jsonl"
        with mock.patch.object(cli, "open_session_source", side_effect=fake_source):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                cli.main(["--json", "render", remote.display, "--output", str(render_output)])
            self.assertEqual(json.loads(stdout.getvalue())["data"]["source"], remote.display)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                cli.main(["query", remote.display, "--kind", "tool_call", "--compact", "--limit", "1"])
            self.assertEqual(json.loads(stdout.getvalue())["kind"], "tool_call")

            with redirect_stdout(io.StringIO()):
                cli.main(["--json", "export", remote.display, "--limit", "1", "--output", str(export_output)])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                cli.main(["--json", "raw", remote.display, "--line", "1"])
            self.assertEqual(json.loads(stdout.getvalue())["data"]["payload"]["id"], ROOT_ID)

            with (
                mock.patch.object(cli.tempfile, "gettempdir", return_value=str(self.root)),
                mock.patch.object(cli.webbrowser, "open", return_value=True) as opener,
                redirect_stdout(io.StringIO()),
            ):
                cli.main(["--json", "browser", remote.display])
            browser_output = self.root / "codex-transcript" / f"robolang-{ROOT_ID}.html"
            opener.assert_called_once_with(browser_output.as_uri())

        for output in (render_output, export_output, browser_output):
            self.assertTrue(output.is_file())
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

        tree = {
            "root_id": ROOT_ID,
            "selected_id": ROOT_ID,
            "nodes": [{"id": ROOT_ID, "selected": True, "depth": 0, "agent_path": "", "thread_source": "user", "path": "robolang:/remote/root.jsonl"}],
        }
        with mock.patch.object(cli, "build_remote_tree", return_value=tree) as builder:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                cli.main(["tree", remote.display, "--format", "json"])
        self.assertEqual(json.loads(stdout.getvalue())["data"]["selected_id"], ROOT_ID)
        builder.assert_called_once_with(remote, None)


if __name__ == "__main__":
    unittest.main()
