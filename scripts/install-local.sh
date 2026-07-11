#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
marketplace="codex-transcript"
plugin="codex-transcript@$marketplace"

uv tool install --editable --force "$root"
codex-transcript --json doctor

if codex plugin marketplace list | awk 'NR > 1 {print $1}' | grep -qx "$marketplace"; then
  codex plugin marketplace remove "$marketplace"
fi

codex plugin marketplace add sadanand1120/codex-transcript-viewer --ref main
codex plugin add "$plugin"

codex plugin marketplace list
codex plugin list
