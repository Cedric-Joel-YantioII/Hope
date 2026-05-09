---
name: hope-skill-create
description: Create a new skill on demand when an existing one doesn't match. Use when the user asks for a capability Hope doesn't already have.
---

# hope-skill-create

On-demand skill creator for Hope. When the user asks for a capability that does not already exist as a Claude Code skill, invoke this to generate, install, and self-test a fresh skill in one pass.

## When to invoke

- The user asks for a capability (e.g. "summarize a PDF into bullet points", "count words in a string", "fetch current weather for a city") and no existing skill description matches.
- You've already checked `~/.claude/skills/` and nothing clearly covers the request.

Do NOT use when:
- A close existing skill handles the request (prefer that skill).
- The user is asking for a one-off command — just run the command.

## How to invoke

```bash
hope-skill create "<natural-language capability description>"
```

Behavior:
1. Indexes existing `SKILL.md` frontmatter into AgentDB namespace `skills-index` (idempotent).
2. Runs `hope-research` scoped to that namespace. If a match scores >= 0.8 it returns `{status: "already_exists", name}` and stops.
3. Otherwise spawns `claude -p` with a generator prompt, writes the result to `~/Documents/Github/Hope/src/skills/generated/<slug>/SKILL.md`, and symlinks it at `~/.claude/skills/<slug>`.
4. Self-tests by spawning `claude -p` on an auto-generated test case; pass/fail goes to AgentDB namespace `hope-skills-eval`.

Output: one JSON object on stdout with `status` (`created` | `already_exists` | `collision` | `error`), `name`, `path`, `self_test.pass`.

## Safety

- Refuses to overwrite any skill that is NOT a symlink into the generated dir (protects Joel's hand-written skills).
- All LLM work goes through `claude -p` (Max subscription auth). No SDK keys.
