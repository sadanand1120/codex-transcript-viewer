"""Build a self-contained HTML viewer from parsed Codex session events."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import resources

from .formatting import format_ts, format_ts_full
from .markdown import escape, render_markdown


def _load_asset(name: str) -> str:
    """Load a bundled CSS or JS asset from the package."""
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def build_html(meta: dict | None, events: list[dict]) -> str:
    """Build a self-contained HTML string from session metadata and events."""
    session_id = meta.get("id", "unknown") if meta else "unknown"
    model = meta.get("model_provider", "") if meta else ""
    cli_version = meta.get("cli_version", "") if meta else ""
    cwd = meta.get("cwd", "") if meta else ""
    branch = meta.get("git", {}).get("branch", "") if meta else ""
    commit = (meta.get("git", {}).get("commit_hash", "") or "")[:12] if meta else ""
    session_ts = meta.get("timestamp", "") if meta else ""
    thread_source = meta.get("thread_source", "") if meta else ""
    source = meta.get("source") if meta else None
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else {}
    spawn = spawn if isinstance(spawn, dict) else {}
    parent_id = spawn.get("parent_thread_id", "")

    sidebar_items: list[str] = []
    message_blocks: list[str] = []
    _render_events(events, sidebar_items, message_blocks)

    css = _load_asset("style.css")
    js = _load_asset("viewer.js")

    sidebar_html = "\n".join(sidebar_items)
    messages_html = "\n".join(message_blocks)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return _HTML_TEMPLATE.format(
        title=escape(session_id[:12]),
        css=css,
        js=js,
        sidebar_html=sidebar_html,
        messages_html=messages_html,
        session_id_short=escape(session_id[:12]),
        session_ts_short=escape(format_ts_full(session_ts)),
        session_id=escape(session_id),
        session_ts=escape(format_ts_full(session_ts)),
        model=escape(model),
        cli_version=escape(cli_version),
        cwd=escape(cwd),
        git_info=escape(branch) + ((" @ " + escape(commit)) if commit else ""),
        thread_source=escape(thread_source),
        parent_id=escape(parent_id),
        generated=generated,
    )


# ---------------------------------------------------------------------------
# Per-event-type rendering functions
# ---------------------------------------------------------------------------

def _render_events(events, sidebar, messages, *, prefix="msg", include_sidebar=True):
    hidden_sidebar: list[str] = []
    for index, evt in enumerate(events, 1):
        handler = _EVENT_HANDLERS.get(evt["type"])
        if handler:
            before = len(messages)
            handler(
                evt,
                format_ts(evt["ts"]),
                f"{prefix}-{index}",
                sidebar if include_sidebar else hidden_sidebar,
                messages,
            )
            if not include_sidebar:
                role = _event_role(evt["type"])
                for position in range(before, len(messages)):
                    messages[position] = (
                        f'<div class="rollback-event {role}">{messages[position]}</div>'
                    )


def _event_role(event_type):
    if event_type == "user_message":
        return "tree-role-user"
    if event_type in {"assistant_text", "agent_commentary", "task_complete", "inter_agent_message"}:
        return "tree-role-assistant"
    if event_type in {"tool_call", "tool_output"}:
        return "tree-role-tool"
    if event_type == "reasoning":
        return "tree-role-thinking"
    if event_type == "turn_aborted":
        return "tree-role-error"
    if event_type == "thread_rolled_back":
        return "tree-role-rollback"
    return "tree-role-system"


def _render_user_message(evt, ts, anchor, sidebar, messages):
    text_preview = evt["text"][:80].replace("\n", " ")
    sidebar.append(
        f'<a class="tree-node tree-role-user" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f464 {escape(text_preview)}</span></a>'
    )
    messages.append(
        f'<div class="user-message" id="{anchor}">'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="markdown-content">{render_markdown(evt["text"])}</div>'
        f"</div>"
    )


def _render_reasoning(evt, ts, anchor, sidebar, messages):
    preview = " ".join(evt["text"].split())[:80]
    sidebar.append(
        f'<a class="tree-node tree-role-thinking" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f4ad {escape(evt["text"][:60])}</span></a>'
    )
    messages.append(
        f'<details class="thinking-block event-details" id="{anchor}">'
        f'<summary class="event-summary">\U0001f4ad Reasoning · {escape(preview)}</summary>'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="thinking-text event-detail-body">{escape(evt["text"])}</div>'
        f"</details>"
    )


def _render_agent_commentary(evt, ts, anchor, sidebar, messages):
    sidebar.append(
        f'<a class="tree-node tree-role-assistant" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f4ac {escape(evt["text"][:60])}</span></a>'
    )
    messages.append(
        f'<div class="commentary-message" id="{anchor}">'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="markdown-content">{render_markdown(evt["text"])}</div>'
        f"</div>"
    )


def _render_assistant_text(evt, ts, anchor, sidebar, messages):
    phase_label = f' ({evt["phase"]})' if evt.get("phase") else ""
    preview = evt["text"][:60].replace("\n", " ")
    is_final = evt.get("phase") == "final_answer"
    icon = "\u2705" if is_final else "\U0001f916"
    final_class = " final-answer" if is_final else ""
    final_label = " \u2014 final answer" if is_final else ""
    sidebar.append(
        f'<a class="tree-node tree-role-assistant" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">{icon} {escape(preview)}</span></a>'
    )
    messages.append(
        f'<div class="assistant-message{final_class}" id="{anchor}">'
        f'<div class="message-timestamp">{ts}{escape(phase_label)}{final_label}</div>'
        f'<div class="assistant-text markdown-content">{render_markdown(evt["text"])}</div>'
        f"</div>"
    )


def _render_tool_call(evt, ts, anchor, sidebar, messages):
    name = str(evt.get("name") or evt.get("raw_type") or "tool")
    arguments = evt.get("arguments", evt.get("input", ""))
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    try:
        args = json.loads(arguments)
        args_preview = args.get("cmd", "")[:80] if name == "exec_command" else json.dumps(args, indent=None)[:80]
    except (json.JSONDecodeError, TypeError):
        args_preview = arguments[:80]

    sidebar.append(
        f'<a class="tree-node tree-role-tool" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u26a1 {escape(name)}: {escape(args_preview)}</span></a>'
    )

    try:
        args_obj = json.loads(arguments)
        if name == "exec_command":
            args_display = f'<span class="tool-command">$ {escape(args_obj.get("cmd", ""))}</span>'
        else:
            args_display = f"<pre>{escape(json.dumps(args_obj, indent=2))}</pre>"
    except (json.JSONDecodeError, TypeError):
        args_display = f"<pre>{escape(arguments)}</pre>"

    messages.append(
        f'<details class="tool-execution event-details" id="{anchor}">'
        f'<summary class="event-summary"><span class="tool-name">{escape(name)}</span>'
        f' · {escape(args_preview)}</summary>'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="tool-args event-detail-body">{args_display}</div>'
        f"</details>"
    )


def _render_tool_output(evt, ts, anchor, sidebar, messages):
    output = evt.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, indent=2)
    preview = " ".join(output.split())[:80]

    sidebar.append(
        f'<a class="tree-node tree-role-tool" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f4e4 output ({len(output)} chars)</span></a>'
    )

    messages.append(
        f'<details class="tool-execution success event-details" id="{anchor}">'
        f'<summary class="event-summary">\U0001f4e4 Output ({len(output):,} chars)'
        f'{f" · {escape(preview)}" if preview else ""}</summary>'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="tool-output event-detail-body"><pre>{escape(output)}</pre></div>'
        f"</details>"
    )


def _render_task_complete(evt, ts, anchor, sidebar, messages):
    preview = evt["text"][:60].replace("\n", " ")
    sidebar.append(
        f'<a class="tree-node tree-role-assistant" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u2705 {escape(preview)}</span></a>'
    )
    messages.append(
        f'<div class="assistant-message final-answer" id="{anchor}">'
        f'<div class="message-timestamp">{ts} \u2014 final answer</div>'
        f'<div class="assistant-text markdown-content">{render_markdown(evt["text"])}</div>'
        f"</div>"
    )


def _render_task_started(evt, ts, anchor, sidebar, messages):
    sidebar.append(
        f'<a class="tree-node tree-role-system" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u25b6 Turn started</span></a>'
    )
    messages.append(
        f'<div class="system-event" id="{anchor}">'
        f'<div class="message-timestamp">{ts}</div>'
        f'<span class="event-label">\u25b6 Turn started</span>'
        f"</div>"
    )


def _render_turn_aborted(evt, ts, anchor, sidebar, messages):
    reason = escape(evt["reason"])
    sidebar.append(
        f'<a class="tree-node tree-role-error" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u26d4 Turn aborted: {reason}</span></a>'
    )
    messages.append(
        f'<div class="system-event error-event" id="{anchor}">'
        f'<div class="message-timestamp">{ts}</div>'
        f'<span class="event-label error-text">\u26d4 Turn aborted: {reason}</span>'
        f"</div>"
    )


def _render_thread_rolled_back(evt, ts, anchor, sidebar, messages):
    n = evt["num_turns"]
    rolled_messages: list[str] = []
    _render_events(
        evt.get("rolled_back_events", []),
        [],
        rolled_messages,
        prefix=f"{anchor}-rolled",
        include_sidebar=False,
    )
    sidebar.append(
        f'<a class="tree-node tree-role-rollback" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u21a9 Rolled back {n} turn(s)</span></a>'
    )
    messages.append(
        f'<details class="rollback-block" id="{anchor}">'
        f'<summary class="rollback-summary">\u21a9 Rolled back {n} turn(s)'
        f' · archived history</summary>'
        f'<div class="message-timestamp">{ts}</div>'
        f'<div class="rollback-content">{"".join(rolled_messages)}</div>'
        f"</details>"
    )


def _render_token_count(evt, ts, anchor, sidebar, messages):
    total = evt["total"]
    if total.get("input_tokens", 0) <= 0:
        return
    tok_str = (
        f"in:{total.get('input_tokens',0):,} "
        f"out:{total.get('output_tokens',0):,} "
        f"reasoning:{total.get('reasoning_output_tokens',0):,}"
    )
    sidebar.append(
        f'<a class="tree-node tree-role-system" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f4ca {tok_str}</span></a>'
    )
    messages.append(
        f'<div class="token-count" id="{anchor}">'
        f'<div class="message-timestamp">{ts}</div>'
        f'<span class="event-label">\U0001f4ca Tokens \u2014 {tok_str}</span>'
        f"</div>"
    )


def _render_subagent_activity(evt, ts, anchor, sidebar, messages):
    agent = escape(evt.get("agent_path") or evt.get("agent_thread_id") or "subagent")
    activity = escape(evt.get("kind") or "activity")
    sidebar.append(
        f'<a class="tree-node tree-role-system" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f500 {agent}: {activity}</span></a>'
    )
    messages.append(
        f'<div class="system-event" id="{anchor}"><div class="message-timestamp">{ts}</div>'
        f'<span class="event-label">\U0001f500 {agent}: {activity}</span></div>'
    )


def _render_inter_agent_message(evt, ts, anchor, sidebar, messages):
    author = escape(evt.get("author") or "agent")
    recipient = escape(evt.get("recipient") or "agent")
    text = evt.get("text", "")
    sidebar.append(
        f'<a class="tree-node tree-role-assistant" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\U0001f4e8 {author} \u2192 {recipient}</span></a>'
    )
    messages.append(
        f'<div class="commentary-message" id="{anchor}"><div class="message-timestamp">'
        f'{ts} \u00b7 {author} \u2192 {recipient}</div>'
        f'<div class="markdown-content">{render_markdown(text)}</div></div>'
    )


def _render_system_detail(evt, ts, anchor, sidebar, messages):
    label = escape(str(evt.get("type", "event")).replace("_", " "))
    detail = evt.get("detail") or evt.get("text") or evt.get("status") or evt.get("raw_type") or ""
    sidebar.append(
        f'<a class="tree-node tree-role-system" href="#{anchor}">'
        f'<span class="tree-ts">{ts}</span> '
        f'<span class="tree-content">\u2022 {label}</span></a>'
    )
    messages.append(
        f'<div class="system-event" id="{anchor}"><div class="message-timestamp">{ts}</div>'
        f'<span class="event-label">{label}</span>'
        f'{f"<pre>{escape(detail)}</pre>" if detail else ""}</div>'
    )


_EVENT_HANDLERS = {
    "user_message": _render_user_message,
    "reasoning": _render_reasoning,
    "agent_commentary": _render_agent_commentary,
    "assistant_text": _render_assistant_text,
    "tool_call": _render_tool_call,
    "tool_output": _render_tool_output,
    "task_complete": _render_task_complete,
    "task_started": _render_task_started,
    "turn_aborted": _render_turn_aborted,
    "thread_rolled_back": _render_thread_rolled_back,
    "token_count": _render_token_count,
    "sub_agent_activity": _render_subagent_activity,
    "inter_agent_message": _render_inter_agent_message,
    "patch_apply_begin": _render_system_detail,
    "patch_apply_end": _render_system_detail,
    "thread_settings_applied": _render_system_detail,
    "web_search_begin": _render_system_detail,
    "web_search_end": _render_system_detail,
    "turn_context": _render_system_detail,
    "world_state": _render_system_detail,
    "compacted": _render_system_detail,
    "inter_agent_communication_metadata": _render_system_detail,
    "unknown": _render_system_detail,
    "parse_error": _render_system_detail,
}


# ---------------------------------------------------------------------------
# HTML shell template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Codex CLI Session \u2014 {title}</title>
  <style>{css}</style>
</head>
<body>
  <button id="hamburger" onclick="document.getElementById('sidebar').classList.toggle('open'); document.getElementById('sidebar-overlay').classList.toggle('open')">\u2630</button>
  <div id="sidebar-overlay" onclick="document.getElementById('sidebar').classList.remove('open'); this.classList.remove('open')"></div>
  <div id="app">
    <aside id="sidebar">
      <div class="sidebar-header">
        <h2>CODEX CLI SESSION</h2>
        <div class="sidebar-meta">{session_id_short} \u00b7 {session_ts_short}</div>
        <input type="text" class="sidebar-search" id="tree-search" placeholder="Filter entries..." oninput="filterTree(this.value)">
        <div class="sidebar-filters">
          <button class="filter-btn active" data-filter="default" onclick="setFilter('default', this)">Default</button>
          <button class="filter-btn" data-filter="no-tools" onclick="setFilter('no-tools', this)">No tools</button>
          <button class="filter-btn" data-filter="user-only" onclick="setFilter('user-only', this)">User</button>
          <button class="filter-btn" data-filter="answers" onclick="setFilter('answers', this)">Answers</button>
          <button class="filter-btn" data-filter="all" onclick="setFilter('all', this)">All</button>
        </div>
      </div>
      <div class="tree-container" id="tree-container">{sidebar_html}</div>
    </aside>
    <main id="content">
      <div class="header">
        <h1><span class="codex-logo">CODEX</span> Session Transcript</h1>
        <div class="header-info">
          <div class="info-item"><span class="info-label">Session ID</span><span class="info-value">{session_id}</span></div>
          <div class="info-item"><span class="info-label">Timestamp</span><span class="info-value">{session_ts}</span></div>
          <div class="info-item"><span class="info-label">Model</span><span class="info-value">{model}</span></div>
          <div class="info-item"><span class="info-label">CLI Version</span><span class="info-value">{cli_version}</span></div>
          <div class="info-item"><span class="info-label">Working Dir</span><span class="info-value">{cwd}</span></div>
          <div class="info-item"><span class="info-label">Git Branch</span><span class="info-value">{git_info}</span></div>
          <div class="info-item"><span class="info-label">Thread Source</span><span class="info-value">{thread_source}</span></div>
          <div class="info-item"><span class="info-label">Parent Thread</span><span class="info-value">{parent_id}</span></div>
        </div>
      </div>
      <div id="messages">{messages_html}</div>
      <div class="footer">Codex CLI session transcript \u00b7 Generated {generated}</div>
    </main>
  </div>
  <script>{js}</script>
</body>
</html>"""
