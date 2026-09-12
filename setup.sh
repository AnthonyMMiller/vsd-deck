#!/usr/bin/env bash
set -euo pipefail
deck_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$deck_root"
sdk_commit=a3c31aa4c330c30fb6d0815c00747ac52374f189
if [[ ! -d vendor/streamdock-sdk ]]; then
  git clone https://github.com/MiraboxSpace/StreamDock-Device-SDK.git vendor/streamdock-sdk
  git -C vendor/streamdock-sdk checkout --detach "$sdk_commit"
else
  installed_commit="$(git -C vendor/streamdock-sdk rev-parse HEAD)"
  if [[ "$installed_commit" != "$sdk_commit" ]]; then
    echo "SDK differs from the tested revision. Preserve your changes and restore $sdk_commit before setup."
    exit 1
  fi
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
echo "Ready. Launch with ./run.sh; use ./run.sh --simulate for a dry run."
