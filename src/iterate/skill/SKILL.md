---
name: hope-iterate
description: Autonomously run tests, spawn a fixer agent on failure, commit when green. Use when the user asks to "make the tests pass", "iterate on this code", "fix the failing tests", or wants an unattended test-fix-commit loop on a project directory.
---

# hope-iterate

Hope's autonomous test → fix → commit loop. Given a project directory, it:

1. Runs the project's test command (autodetected or configured).
2. If red, spawns a `claude -p` fixer subprocess (Max auth, not API key) with the failing output and the files changed since last green. Re-runs tests.
3. Repeats up to `max_iterations` (default 3) with exponential backoff.
4. If green, stages changed files. If `auto_commit: true` (and not on `main`/`master` unless `allow_main` is also true), commits; otherwise prints diff + message for the user to confirm.
5. Logs every cycle to `~/Documents/Github/Hope/.hope-io/iterate.jsonl`.

## When to invoke

- "make the tests pass in `<path>`"
- "iterate on this code until it's green"
- "watch `<path>` and commit when the suite goes green"
- "run the loop on `<path>`"

Do NOT use for interactive debugging — the loop is non-interactive by design.

## How to run

```bash
# One-shot cycle (no watch)
hope-iterate once /path/to/project

# Start a watcher (debounced 2s, pidfile-backed)
hope-iterate start /path/to/project

# Check status
hope-iterate status

# Stop watcher
hope-iterate stop
```

## Optional `.hope-iterate.json`

Place in project root to override defaults:

```json
{
  "test_cmd": "npm test",
  "auto_commit": false,
  "allow_main": false,
  "max_iterations": 3,
  "test_timeout_ms": 300000,
  "commit_message_template": "hope-iterate: green after {iterations} cycle(s)\n\n{summary}"
}
```

## Autodetect order

If `test_cmd` is missing, hope-iterate tries in order:
1. `package.json` with `scripts.test` → `npm test`
2. `pyproject.toml` or `pytest.ini` → `pytest`
3. `Cargo.toml` → `cargo test`
4. `Makefile` with a `test:` target → `make test`

Fails loudly if none match.

## Output

Each cycle emits a JSON report on stdout and an append to `~/Documents/Github/Hope/.hope-io/iterate.jsonl`. On bail, the fixer's in-flight edits are stashed as `hope-iterate-failed-<id>` so the tree stays clean.

## Safety

- Hard cap on fixer iterations (default 3) with exponential backoff.
- Per-cycle test timeout (default 5 min).
- Never auto-commits to `main`/`master` unless BOTH `auto_commit` and `allow_main` are true.
- Never force-pushes, never touches remotes.
- Fixer runs via `claude -p` (CLI subprocess) — inherits Joel's Max auth, never an Anthropic SDK/API key.
