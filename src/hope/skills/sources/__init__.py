"""Skill source resolvers — Hermes, OpenClaw, generic GitHub."""

from hope.skills.sources.base import ResolvedSkill, SourceResolver
from hope.skills.sources.github import GitHubResolver
from hope.skills.sources.hermes import HERMES_REPO_URL, HermesResolver
from hope.skills.sources.openclaw import OPENCLAW_REPO_URL, OpenClawResolver

__all__ = [
    "GitHubResolver",
    "HERMES_REPO_URL",
    "HermesResolver",
    "OPENCLAW_REPO_URL",
    "OpenClawResolver",
    "ResolvedSkill",
    "SourceResolver",
]
