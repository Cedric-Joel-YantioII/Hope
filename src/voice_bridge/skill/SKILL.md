---
name: hope-speak
description: Speak text aloud through Hope's voice. Use when the user asks Hope to say something, or when the assistant wants to vocalize a response. Appends a record to the shared voice-out queue at ~/Documents/Github/Hope/.hope-io/tts-out.jsonl; Hope's tts_consumer picks it up and plays audio via macOS `say`.
---

# hope-speak

A thin producer for Hope's voice-out bridge. Any Claude Code session,
subprocess, or shell that wants Hope to speak appends a line to her
jsonl queue; a long-running consumer (`voice_bridge/tts_consumer.py`)
drains the queue and calls `hope.audio.say.say_sync`.

## When to invoke

Trigger when:
- The user asks Hope to "say", "speak", "read aloud", "announce", or "tell me out loud" something.
- The assistant wants to vocalize a response (e.g. confirmation, status, completion notice) instead of only returning text to the pane.
- A background task finishes and needs to ping Joel audibly.

Do NOT use for:
- Silent acknowledgments — prefer returning text.
- Long-form content. Hope's daemon only speaks the first sentence of any turn; a CLI shim here speaks the full text, which can be disruptive for paragraphs.

## How to run

The CLI lives at `/opt/homebrew/bin/hope-speak` (symlink to
`~/Documents/Github/Hope/bin/hope-speak`).

```bash
# Simple one-liner
hope-speak "Your build is green."

# From stdin (piping output from another command)
git log -1 --pretty=%s | hope-speak --stdin

# Override voice for this utterance
hope-speak --voice Ava "Heads up: meeting in five."

# High priority within the next poll cycle
hope-speak --priority 10 "Urgent: tests failing."
```

Flags:
- `--voice NAME` — macOS voice override (default: `$HOPE_VOICE` or `Samantha`).
- `--priority N` — higher speaks first within a poll cycle (default: 0).
- `--stdin` — read text from stdin instead of argv.

The shim prints the enqueued record's UUID on stdout and exits. The
actual speech happens asynchronously once the consumer picks up the
line (latency ≈ 250 ms poll interval).

## Consumer lifecycle

The consumer is a long-running process. Start it with:

```bash
~/Documents/Github/Hope/scripts/hope-voice-bridge.sh start
~/Documents/Github/Hope/scripts/hope-voice-bridge.sh status
~/Documents/Github/Hope/scripts/hope-voice-bridge.sh stop
```

Or install the launchd plist at `deploy/launchd/com.hope.voice-bridge.plist`
for auto-start on login.

## Wire format

Each queue line:
```json
{"id": "uuid", "text": "…", "voice": "Ava?", "priority": 0, "created_at": "2026-04-21T…Z", "status": "pending"}
```

Completion markers land in `tts-out.done.jsonl` (same directory) so the
consumer never rewrites the producer's append-only log.
