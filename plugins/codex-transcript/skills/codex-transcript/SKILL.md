---
name: codex-transcript
description: Use when inspecting, searching, exporting, or rendering local or SSH-remote Codex CLI JSONL session logs; tracing turns, tool calls, patches, subagents, or inter-agent messages; producing human-readable transcript HTML; or gathering compact structured evidence with the codex-transcript CLI.
---

# Codex Transcript

Use the installed `codex-transcript` command as the deterministic session-log layer. Prefer its structured output over manually scanning raw JSONL or parsing generated HTML.

## Start

```bash
command -v codex-transcript
codex-transcript --json doctor
codex-transcript --json list --limit 10
```

`SESSION` may be a local JSONL path, local session ID/prefix, or `SSH_HOST:SESSION_ID`. Remote references stage the source privately through `ssh-script`; parsing and final outputs remain local.

## Agent analysis

Start narrow and machine-readable:

```bash
codex-transcript query SESSION --kind message --format jsonl --compact
codex-transcript query SSH_ALIAS:SESSION_ID --kind message --format jsonl --compact
codex-transcript query SESSION --turn TURN_ID --format jsonl --compact
codex-transcript query SESSION --tool exec --format jsonl --compact
codex-transcript tree SESSION --format json
```

Use `export` when a durable artifact is needed:

```bash
codex-transcript export SESSION --format jsonl --compact --redact --output session.jsonl
codex-transcript export SESSION --format markdown --compact --output session.md
```

Add `--include-inherited` only when copied parent history inside a subagent log is relevant. Omit `--compact` only when exact raw payloads are required. Unknown records and parse errors remain visible so schema drift is not mistaken for absence.

## Human viewing

Render a reusable private HTML file:

```bash
codex-transcript render SESSION --output transcript.html
```

Open a temporary private viewer in the default browser only when the user wants interactive human viewing:

```bash
codex-transcript browser SESSION
```

Do not invoke `browser` during unattended or remote agent work.

## Rules

- Prefer `--compact` plus focused filters before requesting full raw records.
- Prefer JSONL for evidence extraction and Markdown for concise reading.
- Use `raw SESSION --line N` only when normalized fields are insufficient.
- Prefer `SSH_HOST:SESSION_ID` over manual SSH or copying remote rollout files.
- Treat generated transcripts as sensitive. `--redact` is best-effort, so inspect artifacts before sharing and never publish them without explicit user approval.
- Do not edit or delete source files under `~/.codex/sessions`.
- Do not infer that an event is absent when output contains `kind=unknown` or `kind=parse_error`; inspect the raw record instead.
