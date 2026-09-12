#!/usr/bin/env bash
set -euo pipefail
deck_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$deck_root"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m vsd_deck "$@"
fi
exec python3 -m vsd_deck "$@"
