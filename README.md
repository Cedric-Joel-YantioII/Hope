<div align="center">
  <img alt="Hope" src="assets/Hope_Horizontal_Logo.png" width="400">

  <p><i>Voice-activated, autonomous AI agent — powered by the CLI subscriptions you already pay for.</i></p>

  <p>
    <img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
    <img src="https://img.shields.io/badge/platform-macOS-lightgrey" alt="macOS">
    <img src="https://img.shields.io/badge/brain-claude%20%7C%20gemini%20%7C%20codex-purple" alt="CLI brains">
  </p>
</div>

---

## What this is

**Hope is a fully autonomous, voice-activated personal AI agent that runs locally on your Mac and uses real CLI agent terminals as its brain.** No separate API keys, no extra usage bills.

You speak. Whisper transcribes. A Python daemon decides whether to wake the brain, route the transcript to a tmux pane running `claude --dangerously-skip-permissions`, capture the reply, and pipe the first sentence through macOS `say`. When a task is bigger than a single turn, Hope can spawn sibling specialists (frontend, backend, devops, security, ml-ops, qa, designer, system-designer, ci-cd, researcher, coder, architect) — each a separate `claude`, `gemini`, or `codex` CLI in its own pane — and they coordinate over a Unix-socket bus exactly like a real engineering team. Every spawn lands in a SQLite task journal that survives kills, sleeps, and full daemon restarts; on every wake-up Hope is briefed on what was in flight before the interruption, and abandoned tasks are resumable with their full prior bus history fed back in.

### Why "Hope" over Jarvis-style agents and OpenClaw

Most "personal Jarvis" projects assume you'll bring your own API key and pay per token forever. **OpenClaw** went a step further by reverse-engineering the Claude session token so a self-hosted agent could ride your existing Pro/Max subscription — but Anthropic has since disabled third-party sign-in tokens, and that whole class of project no longer works.

Hope solves the same problem the legitimate way: it drives the **official CLIs** that are already authorised to your account. Each specialist is a real `claude` / `gemini` / `codex` process running in a tmux pane, signed in through the same browser flow you already use. So:

- **No API keys to wire up, no usage bills.** If you're already paying for Anthropic Max, Google Gemini Advanced, or ChatGPT Plus, those subscriptions ARE the cost.
- **Resilient to vendor token policy changes.** OpenClaw broke when Anthropic killed third-party sign-in. Hope can't break that way — it uses the supported, official CLI clients.
- **Multi-vendor by default.** Spawn a Claude researcher and a Gemini coder in the same team; they coordinate over the same bus regardless of which underlying model is doing the thinking.
- **Mid-flight context (the thing native subagents can't do).** Because each specialist is a real CLI process listening on stdin, you can stream new information into a running pane via `hope-send <pane> "..."`. Native Anthropic Task subagents are sealed at dispatch time; tmux-pane CLIs are not.
- **Persistent memory across kills and restarts.** Every spawn opens a row in a SQLite journal; every completed result fans into a semantic RAG store. When Hope wakes back up, she remembers what she was working on. OpenClaw and Jarvis-style projects typically forget the moment the process dies.
- **Voice-first, local-first.** macOS-native. Faster-Whisper for STT, macOS `say` for TTS, no cloud round-trip for the audio pipeline itself.

### Use cases

- **Voice-driven daily driver.** "Hope, take a screenshot and tell me what's on screen." "Hope, open YouTube and play X." "Hope, what time is it?" — answered in your speakers, end-to-end on your Mac.
- **Multi-agent dev squad on demand.** `hope-team "Ship a /signup endpoint with a matching React form" backend frontend ci-cd` spawns three coordinated specialists that exchange the API contract over the bus and report back.
- **Resumable long-running work.** Kill a researcher mid-task, walk away, restart your machine, run `hope-journal resume <task_id>` — the new specialist picks up with the prior conversation already loaded.
- **Mid-flight context injection.** While a coder is implementing, push an updated spec into its pane with `hope-send`. It reads the new context as a normal user turn — no re-spawn needed.

### How it works (architecture in one paragraph)

A long-lived Python daemon (`hope start`, autostarts via launchd) listens to your microphone via Faster-Whisper, gates utterances behind a wake phrase, and forwards them to a brain pane running `claude` in tmux. Replies are scraped off the pane's scrollback (with chrome filtered out) and spoken via macOS `say`. The daemon also exposes a Unix-domain control socket and an inter-pane bus socket; every CLI it spawns gets the bus address baked into its kickoff prompt so specialists can `hope-send`/`hope-ask` each other directly. A SQLite-backed task journal records every spawn at goal-level, an `agent_messages` table preserves every bus message, and a separate ONNX-vector RAG (`hope-rag` MCP server) holds semantic memory. On every wake the daemon reconciles orphaned in-progress tasks to abandoned and ships a briefing line over the bus to the new brain pane.

### Hope CLI surface (after install)

| Command | Job |
|---------|-----|
| `hope-spawn [--cli claude\|gemini\|codex] <role> "<task>"` | Spawn a single specialist in a sibling tmux pane |
| `hope-team "<goal>" <role1> <role2> ...` | Spawn a coordinated squad — each member gets the team roster in its kickoff |
| `hope-send <to> <topic> "<body>"` | Push a one-line bus message to a peer pane (auto-attributes from the calling pane) |
| `hope-ask <peer> "<question>"` | Synchronous question + reply with a correlation_id (default 30s) |
| `hope-agents` / `hope-agents kill <pane>` / `hope-agents kill-all` | List / kill specialists |
| `hope-journal` / `hope-journal show <task>` / `hope-journal resume <task>` | Inspect persistent task memory and resume abandoned work |
| `hope-research "<query>"` | Memory-first knowledge lookup, web fallback, results cached back into RAG |
| `hope-skill list\|create\|evolve` | Manage Hope's self-evolving skill set |
| `hope-see` / `hope-see-recent` / `hope-see-video` | Capture screen / list captures / record short clips |
| `hope-app` / `hope-key` / `hope-click` / `hope-dismiss` | Control any Mac app via accessibility / keyboard / vision-grounded clicks |
| `hope-permissions` | Audit + open the right macOS Privacy panes for Screen Recording + Accessibility |
| `hope-speak "..."` / `hope-talk` | Speak text aloud / drive the voice loop |

### Quick install (macOS)

```bash
git clone https://github.com/open-jarvis/OpenJarvis.git Hope
cd Hope
uv sync
./scripts/build_dashboard.sh    # optional Tauri dashboard

# Authenticate the CLI you want as the default brain
claude          # browser flow once, uses your Anthropic Max plan thereafter
# (optional) gemini, codex — same one-time sign-in each

hope start --detach              # daemon + wake monitor + autolaunch
hope wake                        # spawn the brain pane
hope-permissions                 # grant Screen Recording + Accessibility once
```

Say "Hope, what time is it?" — she answers out loud.

---

## Upstream

Hope was forked from [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) (originally [Hope @ Stanford SAIL](https://scalingintelligence.stanford.edu/blogs/hope/)). The upstream project is a research framework for local-first personal AI; everything below this line documents its general framework features. The voice-loop, CLI-brain orchestration, persistent task journal, multi-agent bus, and `hope-*` tool surface above are the additions that make this fork specifically a *production daily-driver agent built on top of the Anthropic / Google / OpenAI subscription CLIs*.

---

## Why Hope (upstream)?

Personal AI agents are exploding in popularity, but nearly all of them still route intelligence through cloud APIs. Your "personal" AI continues to depend on someone else's server. At the same time, our [Intelligence Per Watt](https://www.intelligence-per-watt.ai/) research showed that local language models already handle 88.7% of single-turn chat and reasoning queries, with intelligence efficiency improving 5.3× from 2023 to 2025. The models and hardware are increasingly ready. What has been missing is the software stack to make local-first personal AI practical.

Hope is that stack. It is an opinionated framework for local-first personal AI, built around three core ideas: shared primitives for building on-device agents; evaluations that treat energy, FLOPs, latency, and dollar cost as first-class constraints alongside accuracy; and a learning loop that improves models using local trace data. The goal is simple: make it possible to build personal AI agents that run locally by default, calling the cloud only when truly necessary. Hope aims to be both a research platform and a production foundation for local AI, in the spirit of PyTorch.

## Installation

### Prerequisites

| Tool | Install |
|------|---------|
| **Python 3.10+** | [python.org](https://www.python.org/downloads/) |
| **uv** (Python package manager) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` — or `brew install uv` on macOS |
| **Rust** | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| **Git** | [git-scm.com](https://git-scm.com/) — or `brew install git` on macOS |

> **macOS users:** see the full [macOS Installation Guide](https://open-hope.github.io/Hope/getting-started/macos/) for a step-by-step walkthrough including Homebrew setup.

### Setup

```bash
git clone https://github.com/open-hope/Hope.git
cd Hope
uv sync                           # core framework
uv sync --extra server             # + FastAPI server

# Build the Rust extension
uv run maturin develop -m rust/crates/hope-python/Cargo.toml
```

> **Python 3.14+:** set `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` before the `maturin` command.

You also need a local inference backend: [Ollama](https://ollama.com), [vLLM](https://github.com/vllm-project/vllm), [SGLang](https://github.com/sgl-project/sglang), or [llama.cpp](https://github.com/ggerganov/llama.cpp). Alternatively, use the `cloud` engine with [OpenAI](https://openai.com), [Anthropic](https://anthropic.com), [Google Gemini](https://ai.google.dev), [OpenRouter](https://openrouter.ai), or [MiniMax](https://www.minimax.io) by setting the corresponding API key environment variable.

## Quick Start

```bash
# 1. Install and detect hardware
git clone https://github.com/open-hope/Hope.git
cd Hope
uv sync
uv run hope init

# 2. Start Ollama and pull a model
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen3:8b

# 3. Ask a question
uv run hope ask "What is the capital of France?"
```

`hope init` auto-detects your hardware and recommends the best engine. Run `uv run hope doctor` at any time to diagnose issues.

### First-time dashboard setup (macOS)

Hope ships a Tauri desktop window that auto-hides on launch and surfaces on "hey Hope". First-time setup builds the `.app` and installs it into `/Applications` so `hope start` can autolaunch it:

```bash
./scripts/build_dashboard.sh
```

After that, `hope start` will bring the window up hidden. If the `.app` isn't installed, the daemon falls back to `npm run tauri dev` (logs tee'd to `~/.hope/dashboard.log`). Both behaviors are controlled by `[dashboard] autolaunch` / `dev_fallback` / `app_bundle_path` in `~/.hope/config.toml`.

## Starter Configs

Install any preset with one command:

```bash
hope init --preset morning-digest-mac   # or any preset below
```

| Preset | Use Case | What it does |
|--------|----------|-------------|
| `morning-digest-mac` | Daily Briefing (Mac) | Spoken briefing from email, calendar, health, news with Hope voice |
| `morning-digest-linux` | Daily Briefing (Linux) | Same, with vLLM support for GPU servers |
| `morning-digest-minimal` | Daily Briefing (minimal) | Just Gmail + Calendar, runs on any machine |
| `deep-research` | Research Assistant | Multi-hop research across indexed docs with citations |
| `code-assistant` | Code Companion | Agent with code execution, file I/O, and shell access |
| `scheduled-monitor` | Persistent Monitor | Stateful agent that runs on a schedule with memory |
| `chat-simple` | Simple Chat | Lightweight conversation, no tools needed |

```bash
# Example: Morning Digest on Mac
hope init --preset morning-digest-mac
hope connect gdrive          # one OAuth flow covers Gmail, Calendar, Tasks
hope digest --fresh           # generate and play your first briefing

# Example: Deep Research
hope init --preset deep-research
hope memory index ./docs/    # index your documents
hope ask "Summarize all emails about Project X"
```

### Skills

Skills teach agents how to better use tools and improve their reasoning. Every skill is a tool — agents discover them from a catalog and invoke them on demand.

```bash
# Install skills from public sources
hope skill install hermes:arxiv
hope skill sync hermes --category research

# Use skills with any agent
hope ask "Use the code-explainer skill to explain this Python code: for i in range(5): print(i*2)"

# Optimize skills from your trace history
hope optimize skills --policy dspy

# Benchmark the impact
hope bench skills --max-samples 5 --seeds 42
```

Import from [Hermes Agent](https://github.com/NousResearch/hermes-agent) (~150 skills), [OpenClaw](https://github.com/openclaw/skills) (~13,700 community skills), or any GitHub repo. Skills follow the [agentskills.io](https://agentskills.io/specification) open standard.

See the [Skills User Guide](https://open-hope.github.io/Hope/user-guide/skills/) and [Skills Tutorial](https://open-hope.github.io/Hope/tutorials/skills-workflow/) for details.

### Built-in Agents

| Agent | Type | What it does |
|-------|------|-------------|
| `morning_digest` | Scheduled | Daily briefing from email, calendar, health, news — with TTS audio |
| `deep_research` | On-demand | Multi-hop research with citations across web and local docs |
| `monitor_operative` | Continuous | Long-horizon monitoring with memory, compression, and retrieval |
| `orchestrator` | On-demand | Multi-turn reasoning with automatic tool selection |
| `native_react` | On-demand | ReAct (Thought-Action-Observation) loop agent |
| `operative` | Continuous | Persistent autonomous agent with state management |
| `native_openhands` | On-demand | CodeAct — generates and executes Python code |
| `simple` | On-demand | Single-turn chat, no tools |

See the [User Guide](https://open-hope.github.io/Hope/user-guide/morning-digest/) and [Tutorials](https://open-hope.github.io/Hope/tutorials/) for detailed setup instructions.

Full documentation — including Docker deployment, cloud engines, development setup, and tutorials — at **[open-hope.github.io/Hope](https://open-hope.github.io/Hope/)**.

## Contributing

We welcome contributions! See the [Contributing Guide](CONTRIBUTING.md) for incentives, contribution types, and the PR process.

Quick start for contributors:

```bash
git clone https://github.com/open-hope/Hope.git
cd Hope
uv sync --extra dev
uv run pre-commit install
uv run pytest tests/ -v
```

Browse the [Roadmap](https://open-hope.github.io/Hope/development/roadmap/) for areas where help is needed. Comment **"take"** on any issue to get auto-assigned.

## About

Hope is part of [Intelligence Per Watt](https://www.intelligence-per-watt.ai/), a research initiative studying the efficiency of on-device AI systems. The project is developed at [Hazy Research](https://hazyresearch.stanford.edu/) and the [Scaling Intelligence Lab](https://scalingintelligence.stanford.edu/) at [Stanford SAIL](https://ai.stanford.edu/).

## Sponsors

<p>
  <a href="https://www.laude.org/">Laude Institute</a> &bull;
  <a href="https://datascience.stanford.edu/marlowe">Stanford Marlowe</a> &bull;
  <a href="https://cloud.google.com/">Google Cloud Platform</a> &bull;
  <a href="https://lambda.ai/">Lambda Labs</a> &bull;
  <a href="https://ollama.com/">Ollama</a> &bull;
  <a href="https://research.ibm.com/">IBM Research</a> &bull;
  <a href="https://hai.stanford.edu/">Stanford HAI</a>
</p>

## Citation
```bibtex
@misc{saadfalcon2026hope,
  title={Hope: Personal AI, On Personal Devices},
  author={Jon Saad-Falcon and Avanika Narayan and Herumb Shandilya and Hakki Orhun Akengin and Robby Manihani and Gabriel Bo and John Hennessy and Christopher R\'{e} and Azalia Mirhoseini},
  year={2026},
  howpublished={\url{https://scalingintelligence.stanford.edu/blogs/hope/}},
}
```

## License

[Apache 2.0](LICENSE)
