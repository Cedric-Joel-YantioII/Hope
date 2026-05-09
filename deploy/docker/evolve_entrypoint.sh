#!/usr/bin/env bash
# Entrypoint for Dockerfile.evolve. Installs the mounted Hope source in
# editable mode (dev extra, *without* speech/capture), then execs the
# supplied command. Keeps voice-only extras out so the image stays small
# and the sandbox genuinely can't reach audio APIs.
set -euo pipefail

cd /workspace

# Install the mounted package; skip speech/capture/vision extras.
# We tolerate re-runs: uv handles already-installed cases.
if [ ! -f /workspace/.evolve_installed ]; then
    uv pip install --system -e '.[dev]' >/tmp/uv_install.log 2>&1 || {
        echo "[evolve] uv install failed; see /tmp/uv_install.log" >&2
        tail -n 50 /tmp/uv_install.log >&2
        exit 1
    }
    touch /workspace/.evolve_installed
fi

# Defensive: make absolutely sure no audio modules are importable.
export HOPE_DISABLE_AUDIO=1
export HOPE_DISABLE_SPEECH=1
export HOPE_DISABLE_CAPTURE=1
export HOPE_NO_WAKEWORD=1

exec "$@"
