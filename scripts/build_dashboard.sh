#!/usr/bin/env bash
# Build the Tauri dashboard and install it into /Applications.
# First-time setup so `hope start` can autolaunch the hidden window.
# Re-run whenever frontend/ changes and you want the production bundle
# to catch up. macOS-only — Tauri emits a .app bundle.
set -euo pipefail

# Make sure cargo is reachable. Without this, tauri-cli silently fails
# with "cargo metadata: No such file or directory" and the script's
# `|| true` swallows it — we'd then "install" the previous stale bundle.
export PATH="$HOME/.cargo/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"
BUNDLE_NAME="Hope.app"
DEST="/Applications/$BUNDLE_NAME"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[build_dashboard] only macOS is supported (got $(uname -s))" >&2
  exit 1
fi
if [[ ! -f "$FRONTEND_DIR/package.json" ]]; then
  echo "[build_dashboard] frontend/package.json not found at $FRONTEND_DIR" >&2
  exit 1
fi

echo "[build_dashboard] installing npm deps ..."
(cd "$FRONTEND_DIR" && npm install)

echo "[build_dashboard] building Tauri release bundle ..."
# Tauri exits non-zero when the updater signing key is missing, even though
# the .app itself bundles successfully. We don't sign for distribution
# (this is a personal local install), so swallow the exit code and rely on
# the BUILT-existence check below to catch real failures.
(cd "$FRONTEND_DIR" && npm run tauri build) || \
  echo "[build_dashboard] (tauri build returned non-zero — checking bundle anyway)"

BUILT=$(/usr/bin/find "$FRONTEND_DIR/src-tauri/target/release/bundle/macos" \
  -maxdepth 2 -name "$BUNDLE_NAME" -print -quit 2>/dev/null || true)
if [[ -z "$BUILT" ]]; then
  echo "[build_dashboard] could not find built $BUNDLE_NAME under src-tauri/target" >&2
  exit 1
fi

echo "[build_dashboard] installing $BUILT → $DEST"
rm -rf "$DEST"
cp -R "$BUILT" "$DEST"

# Remove the build-artefact bundle + dmg + tarball so Spotlight /
# Launchpad don't see two Hope.app icons. They'll be re-created on the
# next `npm run tauri build`.
rm -rf "$BUILT" \
       "$FRONTEND_DIR/src-tauri/target/release/bundle/macos/$BUNDLE_NAME.tar.gz" \
       "$FRONTEND_DIR/src-tauri/target/release/bundle/macos/$BUNDLE_NAME.tar.gz.sig" 2>/dev/null || true
rm -rf "$FRONTEND_DIR/src-tauri/target/release/bundle/dmg" 2>/dev/null || true

echo "[build_dashboard] done. \`hope start\` will now autolaunch the dashboard."
