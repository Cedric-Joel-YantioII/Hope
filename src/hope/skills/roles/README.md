# Hope Role Templates

Every file in this directory is an **ephemeral specialist role** that Hope can
spawn via `TmuxOrchestrator.spawn_specialist(role, task, context)`. A role is
just a Markdown file with YAML frontmatter describing the role and a system
prompt body that will be injected into a freshly spawned Claude Code pane.

Roles are **unbounded**. Anything you drop in here becomes a valid specialist
at runtime — Hope herself is allowed (and encouraged) to author new role
templates as the need arises.

## File format

```markdown
---
name: architect
description: One-sentence blurb shown in listings. Ephemeral specialist.
---
You are Hope's {role} specialist. You have been spawned for a specific task.
Your identity: Hope-{role}. The system is Hope. You are part of her — you are
not Hope herself.

...your role-specific instructions go here...

Publishing protocol:
- When you complete the task, publish a message on topic `to:hope` with your
  findings.
- Correlation id must match the id of the request Hope sent you.
- Then exit so your pane can be reclaimed.

{task-specific context will be injected here at spawn time}
```

The frontmatter is parsed as YAML. Required keys:

| Key         | Meaning                                                 |
|-------------|---------------------------------------------------------|
| `name`      | Canonical role id. Must match the file's basename.      |
| `description` | One-line summary. Used by Hope when choosing a role.  |

The body is the **system prompt** for the spawned pane. The placeholder
`{task-specific context will be injected here at spawn time}` (or any text
containing `{task}` / `{context}`) will be replaced with the concrete task
and serialized context dict when Hope calls `spawn_specialist`.

## Identity invariant

The system as a whole is Hope. Every specialist is `Hope's {role}` and
announces itself as `Hope-{role}`. Only the `hope-main` pane ever speaks as
"I am Hope." Role templates must honour this — if you write a new one, keep
the "you are Hope's X" phrasing.

## Adding a new role at runtime

Hope can write a new markdown file into this directory and spawn it
immediately. There is no registration step and no restart required — the
orchestrator discovers templates by filename on each `spawn_specialist` call.

## Shipped templates

- [`architect.md`](architect.md) — system design, module boundaries, trade-offs
- [`coder.md`](coder.md) — implementation, refactors, tests
- [`researcher.md`](researcher.md) — context gathering from code, docs, web
