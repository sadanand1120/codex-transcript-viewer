# codex-transcript-viewer

Lossless, local inspection for Codex CLI JSONL sessions. One standard-library Python core powers:

- `codex-transcript`, a human and agent-friendly CLI
- self-contained HTML transcripts
- compact JSONL/Markdown exports and focused queries
- parent/subagent session trees
- a Codex plugin with deterministic usage guidance

This is a fork of [masonc15/codex-transcript-viewer](https://github.com/masonc15/codex-transcript-viewer). The original HTML viewer remains the visual foundation.

## Install

Clone the fork, then install the editable CLI and GitHub-backed Codex plugin:

```bash
git clone https://github.com/sadanand1120/codex-transcript-viewer.git
cd codex-transcript-viewer
./scripts/install-local.sh
```

The installer adds the `codex-transcript` marketplace from GitHub and installs `codex-transcript@codex-transcript`.

## Human commands

```bash
codex-transcript list --limit 10
codex-transcript render SESSION --output transcript.html
codex-transcript browser SESSION
```

`browser` writes a deterministic private HTML file under the system temporary directory and opens it with the default browser.

The viewer keeps rolled-back turns under closed archive markers. Tool calls, tool outputs, and reasoning details are also collapsed by default.

## Agent commands

```bash
codex-transcript --json doctor
codex-transcript query SESSION --kind message --format jsonl --compact
codex-transcript query SESSION --turn TURN_ID --compact
codex-transcript export SESSION --format jsonl --compact --redact --output session.jsonl
codex-transcript tree SESSION --format json
codex-transcript raw SESSION --line 42 --redact
```

`SESSION` accepts a JSONL path, exact session ID, or unique ID prefix. Use `list` to discover session IDs.

## Data policy

- Every parsed line receives a versioned normalized envelope.
- Unknown records and malformed JSON remain visible instead of being silently discarded.
- Function and custom tool calls/results retain their `call_id` relationship.
- Subagent identity uses the first `session_meta`; copied parent history is marked `inherited` and excluded by default.
- Raw JSON is preserved by default. `--compact` removes raw known records and truncates large values.
- JSONL export and query stream records instead of loading the whole transcript.

Generated files use owner-only permissions. They may still contain commands, paths, prompts, and tool output. `--redact` performs best-effort redaction of secret-like keys, assignments, and authorization headers; inspect every artifact before sharing.

## Plugin layout

```text
.agents/plugins/marketplace.json
plugins/codex-transcript/.codex-plugin/plugin.json
plugins/codex-transcript/skills/codex-transcript/SKILL.md
```

The CLI is deterministic infrastructure. The plugin teaches Codex to discover sessions, query narrowly, inspect subagent trees, prefer compact structured evidence, and reserve `browser` for human-facing use.

## Development

```bash
uv run --no-project --python 3.11 --with-editable . python -m unittest discover -s tests -v
uv build
```

The runtime has no third-party dependencies.
