from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_transcript_viewer.html_builder import build_html
from codex_transcript_viewer.parser import (
    iter_normalized,
    load_session,
    normalize_entries,
    viewer_projection,
)


def _entry(outer_type: str, payload: dict, timestamp: str = "2026-07-11T13:00:00Z") -> dict:
    return {"timestamp": timestamp, "type": outer_type, "payload": payload}


class NormalizedParserTests(unittest.TestCase):
    def test_retains_modern_records_and_links_both_tool_shapes(self) -> None:
        entries = [
            _entry("session_meta", {"id": "session-1"}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
            _entry("turn_context", {"turn_id": "turn-1", "cwd": "/workspace"}),
            _entry(
                "response_item",
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path":"notes.txt"}',
                    "call_id": "call-1",
                },
            ),
            _entry(
                "response_item",
                {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
            ),
            _entry(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": "text(true);",
                    "call_id": "call-2",
                },
            ),
            _entry(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-2",
                    "output": [{"type": "input_text", "text": "done"}],
                },
            ),
            _entry("world_state", {"full": False, "state": {}}),
            _entry("inter_agent_communication_metadata", {"trigger_turn": True}),
            _entry("compacted", {"replacement_history": []}),
            _entry("event_msg", {"type": "patch_apply_end", "call_id": "patch-1"}),
            _entry("event_msg", {"type": "future_event", "value": 7}),
            _entry("future_outer", {"value": 8}),
        ]

        session = normalize_entries(entries, include_raw=False)

        self.assertEqual(session["schema_version"], 1)
        self.assertEqual(session["meta"]["id"], "session-1")
        self.assertEqual(len(session["events"]), len(entries))
        self.assertEqual(
            [event["kind"] for event in session["events"][3:7]],
            ["tool_call", "tool_output", "tool_call", "tool_output"],
        )
        self.assertEqual(session["events"][3]["paired_seq"], 5)
        self.assertEqual(session["events"][4]["paired_seq"], 4)
        self.assertEqual(session["events"][5]["paired_seq"], 7)
        self.assertEqual(session["events"][6]["paired_seq"], 6)

        common = {
            "kind",
            "seq",
            "line",
            "timestamp",
            "session_id",
            "parent_session_id",
            "turn_id",
            "origin",
            "outer_type",
            "raw_type",
        }
        self.assertTrue(all(common <= event.keys() for event in session["events"]))
        self.assertEqual(session["events"][7]["kind"], "world_state")
        self.assertNotIn("raw", session["events"][7])
        self.assertEqual(session["events"][-2]["kind"], "unknown")
        self.assertIn("raw", session["events"][-2])
        self.assertEqual(session["events"][-1]["raw_type"], "future_outer")
        self.assertIn("raw", session["events"][-1])

        _meta, projected = viewer_projection(session)
        unknown = [event for event in projected if event["type"] == "unknown"]
        self.assertTrue(any('"value": 7' in event.get("detail", "") for event in unknown))
        self.assertIn("future_event", build_html({"id": "session-1"}, projected))

    def test_subagent_prefix_origin_and_first_meta_identity(self) -> None:
        child_id = "019f5157-4898-78e3-b799-2d9f5f7e5e5f"
        parent_id = "019f5129-bbdd-75f0-af39-33a25b64b9ad"
        entries = [
            _entry(
                "session_meta",
                {
                    "id": child_id,
                    "timestamp": "2026-07-11T13:21:42.552Z",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
                        }
                    },
                },
            ),
            _entry("session_meta", {"id": parent_id}),
            _entry(
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": "019f5000-0000-7000-8000-000000000001",
                    "started_at": 1783770000,
                },
            ),
            _entry(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "copied parent turn"}],
                },
            ),
            _entry(
                "response_item",
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "child setup"}],
                },
            ),
            _entry(
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": "019f5157-5279-7d51-8f4e-f73a933f9e01",
                    "started_at": 1783776105,
                },
            ),
            _entry("world_state", {"full": False, "state": {}}),
        ]

        complete = normalize_entries(entries, include_inherited=True)
        native = normalize_entries(entries, include_inherited=False)

        self.assertEqual(complete["meta"]["id"], child_id)
        self.assertEqual(complete["parent_session_id"], parent_id)
        self.assertEqual(
            [event["origin"] for event in complete["events"]],
            ["native", "inherited", "inherited", "inherited", "native", "native", "native"],
        )
        self.assertEqual([event["seq"] for event in native["events"]], [1, 5, 6, 7])
        self.assertTrue(all(event["session_id"] == child_id for event in complete["events"]))
        self.assertTrue(all(event["parent_session_id"] == parent_id for event in native["events"]))

    def test_started_at_fallback_finds_native_boundary(self) -> None:
        entries = [
            _entry(
                "session_meta",
                {
                    "id": "non-uuid-child",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "parent_thread_id": "parent-1",
                },
            ),
            _entry(
                "event_msg",
                {"type": "task_started", "turn_id": "old", "started_at": 1767225500},
            ),
            _entry(
                "event_msg",
                {"type": "task_started", "turn_id": "new", "started_at": 1767225601},
            ),
        ]

        session = normalize_entries(entries, include_inherited=False)
        self.assertEqual([event["seq"] for event in session["events"]], [1, 3])
        self.assertEqual(session["warnings"], [])

    def test_unresolved_subagent_boundary_is_retained_as_unknown(self) -> None:
        entries = [
            {
                "type": "session_meta",
                "payload": {"id": "child", "parent_thread_id": "parent"},
            },
            _entry(
                "event_msg",
                {"type": "task_started", "turn_id": "not-a-uuid"},
            ),
            _entry("event_msg", {"type": "agent_message", "message": "possibly native"}),
        ]

        session = normalize_entries(entries, include_inherited=False)

        self.assertEqual(
            [event["origin"] for event in session["events"]],
            ["native", "unknown", "unknown"],
        )
        self.assertEqual(session["warnings"], ["subagent native boundary not found"])

    def test_load_and_stream_preserve_parse_errors_and_unknown_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(_entry("session_meta", {"id": "session-1"})),
                        "",
                        "{not-json",
                        "42",
                        json.dumps(_entry("unknown_outer", {"secret": "kept"})),
                    ]
                ),
                encoding="utf-8",
            )

            session = load_session(path, include_raw=False)
            streamed = list(iter_normalized(path, include_raw=False))

        self.assertEqual(session["events"], streamed)
        self.assertEqual([event["seq"] for event in streamed], [1, 2, 3, 4])
        self.assertEqual([event["line"] for event in streamed], [1, 3, 4, 5])
        self.assertEqual(streamed[1]["kind"], "parse_error")
        self.assertEqual(streamed[1]["raw"], "{not-json")
        self.assertEqual(streamed[2]["kind"], "unknown")
        self.assertEqual(streamed[2]["raw"], 42)
        self.assertIn("invalid JSON", session["warnings"][0])

    def test_viewer_projection_reconciles_exact_duplicates(self) -> None:
        entries = [
            _entry("session_meta", {"id": "session-1"}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
            _entry("event_msg", {"type": "user_message", "message": "hello"}),
            _entry(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            ),
            _entry("event_msg", {"type": "agent_message", "message": "working"}),
            _entry(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "working"}],
                },
            ),
            _entry("event_msg", {"type": "agent_reasoning", "text": "reason"}),
            _entry(
                "response_item",
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "reason"}],
                },
            ),
            _entry(
                "response_item",
                {"type": "custom_tool_call", "name": "exec", "input": "x", "call_id": "c1"},
            ),
            _entry(
                "response_item",
                {"type": "custom_tool_call_output", "output": "ok", "call_id": "c1"},
            ),
            _entry("event_msg", {"type": "patch_apply_end", "call_id": "patch-1"}),
            _entry(
                "response_item",
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/parser",
                    "content": [{"type": "input_text", "text": "new task"}],
                },
            ),
            _entry("inter_agent_communication_metadata", {"trigger_turn": True}),
            _entry("event_msg", {"type": "task_complete", "last_agent_message": "done"}),
            _entry(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ),
        ]

        meta, events = viewer_projection(normalize_entries(entries))
        _compact_meta, compact_events = viewer_projection(
            normalize_entries(entries, include_raw=False)
        )
        types = [event["type"] for event in events]
        compact_types = [event["type"] for event in compact_events]

        self.assertEqual(meta["id"], "session-1")
        self.assertEqual(types.count("user_message"), 1)
        self.assertEqual(types.count("assistant_text"), 2)
        self.assertEqual(types.count("reasoning"), 1)
        self.assertEqual(types.count("tool_call"), 1)
        self.assertEqual(types.count("tool_output"), 1)
        self.assertIn("patch_apply_end", types)
        self.assertIn("inter_agent_message", types)
        self.assertIn("inter_agent_communication_metadata", types)
        self.assertNotIn("task_complete", types)
        self.assertEqual(compact_types, types)

    def test_viewer_projection_archives_only_currently_active_rolled_back_turns(self) -> None:
        entries = [
            _entry("session_meta", {"id": "session-1"}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-a"}),
            _entry("event_msg", {"type": "user_message", "message": "active A"}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-b"}),
            _entry("event_msg", {"type": "user_message", "message": "archived B"}),
            _entry(
                "response_item",
                {"type": "custom_tool_call", "name": "exec", "input": "archived tool"},
            ),
            _entry("event_msg", {"type": "thread_rolled_back", "num_turns": 1}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-c"}),
            _entry("event_msg", {"type": "user_message", "message": "active C"}),
            _entry("event_msg", {"type": "thread_rolled_back", "num_turns": 2}),
            _entry("event_msg", {"type": "task_started", "turn_id": "turn-d"}),
            _entry("event_msg", {"type": "user_message", "message": "active D"}),
        ]

        _meta, events = viewer_projection(normalize_entries(entries))
        top_level_messages = [
            event["text"] for event in events if event["type"] == "user_message"
        ]
        rollbacks = [event for event in events if event["type"] == "thread_rolled_back"]

        self.assertEqual(top_level_messages, ["active D"])
        self.assertEqual(
            [event["text"] for event in rollbacks[0]["rolled_back_events"] if event["type"] == "user_message"],
            ["archived B"],
        )
        self.assertEqual(
            [event["text"] for event in rollbacks[1]["rolled_back_events"] if event["type"] == "user_message"],
            ["active A", "active C"],
        )
        self.assertNotIn("_turn_seq", json.dumps(events))

        html = build_html({"id": "session-1"}, events)
        self.assertIn('class="rollback-block"', html)
        self.assertIn('class="rollback-content"', html)
        self.assertIn('class="tree-node tree-role-rollback"', html)
        self.assertIn('class="rollback-event tree-role-tool"', html)
        self.assertIn("function archiveVisible", html)
        self.assertNotIn('class="rollback-block" open', html)
        self.assertIn("archived B", html)


if __name__ == "__main__":
    unittest.main()
