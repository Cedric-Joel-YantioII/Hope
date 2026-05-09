#!/usr/bin/env bash
# evolve-loop.sh — opt-in background evolver for Hope skills.
#
# Default: DISABLED. Joel flips HOPE_SKILL_EVOLVER_ENABLED=1 in his env (or in
# a launchd plist) to turn this on. When enabled, this script loops every
# HOPE_SKILL_EVOLVE_INTERVAL_SEC (default 3600s) and runs scan-evolve with
# --auto-promote. It writes a heartbeat to logs/hope-skill-evolver.log.
#
# Promotion rules are enforced inside hope_skill_evolve.mjs:
#   - candidate beats current on BOTH p95 and success_rate (with success >= 0.8)
#   - current gets backed up to SKILL.md.v<n>.bak before swap
#   - hand-written skills are never touched (generated-dir guard)
#
# To enable: export HOPE_SKILL_EVOLVER_ENABLED=1 and run this script (or wire
# into launchd with a RunAtLoad plist that sets the env var).

set -euo pipefail

ENABLED="${HOPE_SKILL_EVOLVER_ENABLED:-0}"
INTERVAL="${HOPE_SKILL_EVOLVE_INTERVAL_SEC:-3600}"
LOG="/Users/joelc/Documents/Github/Hope/logs/hope-skill-evolver.log"
CLI="/opt/homebrew/bin/hope-skill"

mkdir -p "$(dirname "$LOG")"

if [[ "$ENABLED" != "1" ]]; then
  echo "$(date -Iseconds) evolver disabled (set HOPE_SKILL_EVOLVER_ENABLED=1 to enable)" >> "$LOG"
  exit 0
fi

while true; do
  echo "$(date -Iseconds) scan-evolve start" >> "$LOG"
  if "$CLI" scan-evolve --auto-promote >> "$LOG" 2>&1; then
    echo "$(date -Iseconds) scan-evolve ok" >> "$LOG"
  else
    echo "$(date -Iseconds) scan-evolve error (continuing)" >> "$LOG"
  fi
  sleep "$INTERVAL"
done
