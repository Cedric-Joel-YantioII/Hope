"""Human-gated merge of a proposed evolution branch into ``main``.

Phase-1 rule: *never* auto-merge. The runner only creates an
``evolve/merged-<ts>`` branch; this module turns that branch into a commit
on ``main`` (or whatever the target branch is) but only when called
explicitly — either by ``hope evolve approve <branch>`` or by a human PR
tool. The evolution runner never invokes anything in here on its own.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_TARGET = "main"


class MergeError(RuntimeError):
    """Raised when a merge is blocked by safety checks."""


def approve_and_merge(
    branch: str,
    *,
    repo_path: Path,
    target_branch: str = DEFAULT_TARGET,
    squash: bool = True,
    extra_trailer: Optional[str] = None,
) -> str:
    """Squash-merge an ``evolve/merged-<ts>`` branch into ``target_branch``.

    Returns the new commit SHA on ``target_branch``.

    Safety checks enforced:

    * ``branch`` must exist and start with ``evolve/merged-``.
    * The working tree must be clean (no uncommitted changes on
      ``target_branch``) — never silently overwrite a human's work.
    * Uses ``git merge --squash`` so the history stays flat and a single
      reviewable commit lands on ``target_branch``.

    We deliberately do *not* push: pushing is a separate manual step.
    """
    if not branch.startswith("evolve/merged-"):
        raise MergeError(
            f"refusing to merge branch {branch!r}: must start with 'evolve/merged-'"
        )

    # Verify branch exists.
    try:
        _git(repo_path, "rev-parse", "--verify", branch)
    except subprocess.CalledProcessError as exc:
        raise MergeError(f"branch {branch!r} does not exist") from exc

    # Check working tree is clean.
    status = _git(repo_path, "status", "--porcelain").strip()
    if status:
        raise MergeError(
            f"working tree is dirty; refusing to merge:\n{status[:400]}"
        )

    # Switch to target.
    _git(repo_path, "checkout", target_branch)

    # Capture the subject line of the source branch for the final commit.
    subject = _git(repo_path, "log", "-1", "--pretty=%s", branch).strip()
    body = _git(repo_path, "log", "-1", "--pretty=%b", branch).strip()

    commit_msg = f"{subject}\n\n{body}\n\nMerged-from: {branch}"
    if extra_trailer:
        commit_msg += f"\n{extra_trailer}"

    if squash:
        _git(repo_path, "merge", "--squash", branch)
        # `merge --squash` does not create a commit on its own.
        _git(repo_path, "commit", "-m", commit_msg)
    else:
        _git(repo_path, "merge", "--no-ff", "-m", commit_msg, branch)

    sha = _git(repo_path, "rev-parse", "HEAD").strip()
    logger.info("evolution: merged %s into %s as %s", branch, target_branch, sha)
    return sha


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


__all__ = ["DEFAULT_TARGET", "MergeError", "approve_and_merge"]
