---
name: auto-research
description: Memory-first, web-fallback research via Hope's auto-research module. Use when the user asks to research, look-up, investigate, explore, or find-out something. Hits Joel's AgentDB + Claude Code memory first; falls back to web only if local memory has no strong match. Results are cached back to AgentDB for future queries.
---

# auto-research

Hope ships a research helper that searches Joel's persistent memory before touching the web, then caches any web-sourced synthesis so the same query hits memory next time.

## When to invoke

Trigger on natural-language requests like:
- "research X", "look into Y", "investigate Z"
- "what do you know about X" (when /recall alone is too narrow)
- "find out the latest on X"
- anything where fresh factual grounding would help

Do NOT use for:
- personal-memory questions already covered by `/recall` or `/self-report`
- code analysis (use `deepdive` or `researcher` agent instead)

## How to run

Invoke the CLI from Bash:

```bash
hope-research "your query here"
```

Optional flags:
- `--threshold 0.8` — raise memory-hit bar (default 0.75)
- `--top-k 5` — how many hits to return
- `--pretty` — human-readable JSON

## Output shape

Single JSON object on stdout:

```json
{
  "query": "...",
  "route": "memory" | "web",
  "top_score": 0.0,
  "answer": "synthesised answer or top memory value",
  "hits": [{ "source": "agentdb" | "claude-memory" | "research-cache" | "web", "key": "...", "value": "...", "score": 0.0 }],
  "persisted": { "key": "...", "namespace": "hope-research", "cachePath": "..." } | null,
  "elapsed_ms": 123
}
```

## Parsing pattern

```bash
RESULT=$(hope-research "PRISM Nigeria GTM")
ROUTE=$(echo "$RESULT" | jq -r '.route')
ANSWER=$(echo "$RESULT" | jq -r '.answer')
```

Then summarise `.answer` to the user and optionally list `.hits[].source` + `.hits[].key` as citations.

## Behaviour notes

- Memory hits at score >= 0.75 short-circuit the web path entirely
- Web fallback uses DuckDuckGo instant answers + HTML extraction (no API key)
- All web-sourced answers auto-persist to AgentDB namespace `hope-research` AND a local JSON cache at `~/Documents/Github/Hope/.claude-flow/research-cache/`
- Silent-by-default: no stdout noise, no progress logs — only the final JSON
- Do not invoke from inside Hope's voice loop; this is for CC CLI use

## Making this globally discoverable

The harness blocked direct writes to `~/.claude/skills/auto-research/`. To surface this skill to every CC session, Joel should run once:

```bash
mkdir -p ~/.claude/skills
ln -s /Users/joelc/Documents/Github/Hope/src/research/skill ~/.claude/skills/auto-research
```

After that, `/auto-research` will appear in any CC session's skill picker.
