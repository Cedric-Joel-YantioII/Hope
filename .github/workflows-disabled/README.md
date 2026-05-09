# Disabled workflows (parked, not removed)

These workflows ship with the upstream Stanford Hope project but require
infrastructure this fork doesn't have:

- `docs.yml` — deploys mkdocs to GitHub Pages; needs a `docs` extra in
  pyproject.toml that the upstream tracks separately.
- `pypi-publish.yml` — publishes to upstream's PyPI account; needs
  `PYPI_API_TOKEN` secret owned by the Stanford project.
- `desktop.yml` — Tauri build + release; needs Apple Developer + Tauri
  signing certificates we don't have on this fork.
- `track-clones.yml` — pushes clone-stats to a Stanford-specific Supabase
  project.
- `claude-issues.yml` / `claude-review.yml` — Claude bot for issue triage
  and PR review; needs an `ANTHROPIC_API_KEY` secret.

Re-enable any of them by moving the file back to `.github/workflows/`
and adding the corresponding secret(s) in repo Settings → Secrets.
