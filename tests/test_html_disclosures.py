from __future__ import annotations

import unittest

from codex_transcript_viewer.html_builder import build_html


class HtmlDisclosureTests(unittest.TestCase):
    def test_internal_events_are_collapsed_but_conversation_stays_open(self) -> None:
        events = [
            {"type": "user_message", "ts": "", "text": "visible prompt", "images": []},
            {"type": "reasoning", "ts": "", "text": "private reasoning detail"},
            {"type": "tool_call", "ts": "", "name": "exec", "arguments": '{"cmd":"rg secret"}'},
            {"type": "tool_output", "ts": "", "output": "full <output>"},
            {"type": "assistant_text", "ts": "", "text": "visible answer", "phase": "final_answer"},
        ]

        html = build_html({"id": "session-1"}, events)

        self.assertEqual(html.count('class="tool-execution event-details"'), 1)
        self.assertEqual(html.count('class="tool-execution success event-details"'), 1)
        self.assertEqual(html.count('class="thinking-block event-details"'), 1)
        self.assertNotIn('event-details" open', html)
        self.assertIn("private reasoning detail", html)
        self.assertIn("rg secret", html)
        self.assertIn("full &lt;output&gt;", html)
        self.assertIn('<div class="user-message"', html)
        self.assertIn('<div class="assistant-message final-answer"', html)


if __name__ == "__main__":
    unittest.main()
