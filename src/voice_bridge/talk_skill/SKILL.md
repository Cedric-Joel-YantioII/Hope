---
name: hope-talk
description: Start/stop Hope's conversational turn loop — the voice-in bridge that listens for finalized transcripts, spawns the Claude Code CLI to generate a reply, and queues it for Hope's TTS. Use when the user says "Hope, listen", "Hope, start listening", "begin a voice session", "open a voice chat", or otherwise asks to begin/stop a live conversational session with Hope.
---

# hope-talk — Hope's voice-in bridge controller

This skill runs `/opt/homebrew/bin/hope-talk` to manage the voice-in
daemon at `~/Documents/Github/Hope/src/voice_bridge/turn_loop.py`.

## What the daemon does

On start, it tails Hope's daemon log (`~/.hope/daemon.log`) for
`[HEARD] '...'` markers (which Hope writes for every finalized STT
transcript). For each transcript it:

1. Debounces for 800 ms in case a correction arrives.
2. Spawns `claude -p "<transcript>" --output-format text` — this
   inherits Joel's logged-in Max subscription auth from the Claude Code
   CLI's cached credentials. Never uses an Anthropic SDK API key.
3. Appends the reply to the shared TTS-out queue at
   `~/Documents/Github/Hope/.hope-io/tts-out.jsonl` (consumed by the
   voice-out bridge agent's `tts_consumer.py`).
4. Writes a full trace line to
   `~/Documents/Github/Hope/.hope-io/turns.jsonl` with
   `{turn_id, transcript, reply, error, exit_code, latency_ms,
   timestamp}` for the `self-report` skill to query later.

On non-zero `claude` exit, it queues `"Sorry, I hit an error"` to the
TTS-out queue and records the error in the turn log.

## Subcommands

Run exactly one of:

| Command                | Effect                                                 |
| ---------------------- | ------------------------------------------------------ |
| `hope-talk start`      | Boot the daemon. Safe to call repeatedly.              |
| `hope-talk stop`       | SIGTERM the daemon; SIGKILL after 5 s if unresponsive. |
| `hope-talk status`     | Print pid, liveness, queue paths, and turn count.      |
| `hope-talk tail`       | Follow `turns.jsonl` (one line per turn).              |
| `hope-talk tail --log` | Follow the daemon's own `turn_loop.log`.               |

## Typical triggers

- "Hope, listen" / "start listening" → `hope-talk start`
- "Hope, stop listening" / "that's enough" → `hope-talk stop`
- "How's the voice loop?" / debugging → `hope-talk status`, then
  `hope-talk tail` if the user wants a live trace.

## Coordination with other bridges

The `hope-talk` loop is a **producer** for the voice-out queue at
`.hope-io/tts-out.jsonl`. The companion `hope-speak` skill (also a
producer) lets any shell/subprocess enqueue speech on demand. The
consumer (`voice_bridge/tts_consumer.py`) is owned by the voice-out
bridge and must be running for Hope to actually speak.

## Notes

- The daemon runs independently of Hope's tmux-based brain pane. It's a
  second path from transcript → brain that uses a fresh `claude`
  subprocess per turn (stateless) rather than a long-lived tmux pane.
- Pidfile: `~/Documents/Github/Hope/.hope-io/turn_loop.pid`.
- Never touch Hope's `.claude/` directory or `~/.claude/settings.json`
  when working on this skill — those stubs and the dangerous-mode flag
  are intentional (see `~/.claude/projects/-Users-joelc/memory/project_hope.md`).
