<div align="center">
  <img alt="Hope" src="assets/Hope_Horizontal_Logo.png" width="400">

  <p><i>The voice assistant Siri and Alexa should have been.</i></p>

  <p>
    <img src="https://img.shields.io/badge/platform-macOS-lightgrey" alt="macOS">
    <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
    <img src="https://img.shields.io/badge/no%20API%20key-required-blue" alt="No API key required">
  </p>
</div>

---

## What is Hope

Hope is a voice-activated AI assistant that lives on your Mac. You talk, she listens, she does the thing. She can open apps, play music, send messages, take screenshots, look at your camera, search the web, and run real work for you in the background. She remembers your conversations, your people, and your projects.

You don't need an API key, and there's no per-question bill.

## Why Hope exists (and why she doesn't cost extra)

If you're already paying for **Claude (Anthropic Pro/Max)**, **ChatGPT (OpenAI Plus)**, or **Gemini Pro (Google)**, those subscriptions ARE Hope's brain. She drives the official command-line apps for whichever services you're signed into — `claude`, `gemini`, `codex` — through the same browser sign-in you already use. **Your subscription is the cost.** No new API key, no new meter.

This project started out of a simple frustration. There was a great tool called **OpenClaw** that let people use their Claude subscription in custom AI agents by using sign-in tokens. Anthropic disabled those tokens for third-party use, and that whole class of project broke overnight. Hope is the version that can't break that way — she only uses the official, supported CLIs that Anthropic, Google, and OpenAI ship themselves.

## What she can do

- **Real conversation, not commands.** Talk to her in full sentences. After her first reply, you don't even need to say her name again — just keep talking.
- **Take action on your Mac.** Open apps, control music, type, click, search, send messages — anything you'd do yourself.
- **See what you see.** Ask her to take a screenshot or look through your webcam and she will, then tell you what she sees.
- **Look things up.** If she doesn't know, she searches the web and reports back.
- **Spawn a team.** For real work she launches additional AI agents — a researcher, a coder, a designer — each one a *full* Claude Code / Gemini / Codex instance with full system access. They communicate with each other like a real team, not like sealed-off helpers.
- **Remember.** Long-term memory of conversations, projects, people, and commitments. If she gets killed mid-task, she resumes where she left off when she comes back up.
- **Build her own skills.** When she figures out a workflow that works, she writes it down. Next time the same task comes up, she's faster. Skills evolve themselves over time. She also has MCP tools available out of the box, and an auto-research module so she can teach herself things.

## How to deploy her

Mac-only for now. Apple Silicon recommended. There's a fair bit to wire up — Python dependencies, a Rust extension, audio models for hearing and speaking, macOS permissions, the desktop app, Login Items so she boots with your Mac. **You don't have to do any of that yourself.**

### Step 1 — clone the repo

```bash
git clone https://github.com/Cedric-Joel-YantioII/Hope.git
cd Hope
```

### Step 2 — hand it to Claude Code

Open Claude Code (or Cursor, or any AI coding tool) in the cloned folder and tell it:

> *"Set up Hope from scratch on this Mac. Start by reading `CLAUDE.md`, `AGENTS.md`, and `SOUL.md` in this repo — those files tell you everything about how she works. Then install all Python and Rust dependencies, download the audio models (Whisper for hearing, Kokoro for speaking), build the Tauri desktop app and install it into `/Applications`, sign me into the `claude` CLI through the browser, set up the macOS Privacy permissions she needs (Microphone, Screen Recording, Accessibility), and add Hope.app to my Login Items so she starts when my Mac does. When you're finished, tell me to say "Wake up, Hope" and confirm I hear her reply."*

That single instruction is enough. Claude Code reads the CLAUDE.md / AGENTS.md / SOUL.md files in the repo (they were written for exactly this purpose) and walks the rest of the setup itself.

### Step 3 — talk to her

Once Claude Code says it's done:

> **You:** "Wake up, Hope."
>
> **Hope:** "I'm here, sir."
>
> **You:** "What's the time?"
>
> **Hope:** *(answers)*

That's it.

## Credits

Hope is forked from [**OpenJarvis**](https://github.com/open-jarvis/OpenJarvis), which is itself derived from the [**Hope** research framework at Stanford SAIL](https://scalingintelligence.stanford.edu/blogs/hope/). The upstream is a general-purpose local-first personal-AI framework; this fork is the voice-activated, CLI-brain, multi-agent personal-assistant layer built on top of it. Huge thanks to the original authors for the foundation — without their work this wouldn't exist.

## License

[Apache 2.0](LICENSE) — inherited from upstream.
