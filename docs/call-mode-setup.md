# Call mode — how Hope behaves on a call

**v1 (default, zero install): mic-only + visual cues.** When `hope-call-detect`
returns a non-empty slug (zoom / facetime / teams / webex / slack-huddle /
discord-call), Hope is on a call. She does **two** things:

1. **Listens to your side via the always-on mic.** Whatever you say lands in
   the `call_notes` AgentDB namespace under the active `session_id` (see
   `src/hope/memory/call_notes.py`). Speaker tag is left unset — there's
   only one audio source, so labelling everything "me" would be noise.
2. **Uses visual cues for the rest.** When asked ("what did Alex just say?",
   "brief me on this call"), she calls `hope-see --window <call-app>` to
   read the call window directly: shared slides, live captions if the app
   shows them, who's currently on camera. For longer context she can use
   `hope-see-video <seconds>` to grab a short clip and step through frames.

That's the whole pipeline for v1. No extra software, no audio routing
changes, no hardware.

## What you give up

- **No automatic remote-speaker transcription.** If you want Hope to know
  what the other person said, either paraphrase it out loud ("so Alex just
  said X — note that"), let her read the call app's built-in captions
  visually, or paste a chunk of the app's transcript into the chat.
- **No `speaker="remote"` notes.** Everything in `call_notes` for v1 is
  implicitly your side.

## What still works

- `hope-call-detect` — slug-based active-call detection.
- `call_notes.save_note(session_id, text, ...)` — the `speaker` and `tag`
  fields stay optional and accept `None` cleanly.
- The proactive-recall + nightly-consolidator loops still run during calls;
  Hope just won't volunteer mid-call (quiet-hours-style guard).

## Future upgrade — opt-in two-track audio

If one day you want Hope to hear the remote side too, the unlock is a free
virtual audio driver:

1. `brew install blackhole-2ch`
2. **Audio MIDI Setup** → **+** → **Create Multi-Output Device** → tick
   both your normal output and *BlackHole 2ch* → set as system output. You
   still hear the call; BlackHole captures a mirror.
3. Hope spawns a second `faster-whisper` instance against `BlackHole 2ch`
   and tags those utterances `speaker="remote"`.

That's a 30-second install when (and if) you want it. Until then, ignore
this section.

macOS 13+ also exposes system audio natively via **ScreenCaptureKit**'s
`SCContentFilter` audio stream — when Hope grows a Swift bridge, BlackHole
becomes unnecessary even for the upgrade path.
