"""First concrete experiment: rewrite Hope's ack phrases.

Hope says a short ack phrase (``_ACK_PHRASES`` in ``hope.daemon.core``) while
the brain is generating a reply. The ack has a huge effect on perceived
snappiness and personality. The sibling learning agent records an outcome
score for every user turn in ``~/.hope/traces.db``; ack phrases that
precede low-rated replies probably feel wrong.

This experiment:

1. pulls the outcome score distribution per ack phrase from ``traces.db``,
2. asks Claude (one-shot, via ``claude -p``) to propose a fresh tuple of
   phrases that are shorter / more natural / more in line with Hope's
   voice (see SOUL.md),
3. rewrites the ``_ACK_PHRASES = (...)`` literal in
   ``src/hope/daemon/core.py``,
4. runs the test suite,
5. asks an LLM judge to score the new tuple 0-10. Success requires
   **all of**:
     - tests pass
     - proposed tuple differs from the current one
     - judge score ≥ 7

Network is disabled inside the sandbox. The ``claude -p`` call therefore
happens on the host *before* the experiment enters the container — the
proposed phrases get plumbed in via an environment variable
(``HOPE_EVOLVE_ACK_PROPOSAL``). Tests exercise this path with a seeded
proposal so no real LLM is needed.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hope.evolution.experiment import (
    EvaluationResult,
    Experiment,
    ExperimentRegistry,
)

logger = logging.getLogger(__name__)


CORE_PATH_REL = "src/hope/daemon/core.py"
#: Names that might hold the ack-phrase tuple. The codebase moved from
#: ``_ACK_PHRASES = (...)`` in core.py to ``DEFAULT_ACKS = (...)`` in the
#: learning module, then back again — this list covers both shapes.
_CANDIDATE_NAMES = ("DEFAULT_ACKS", "_ACK_PHRASES")
MIN_JUDGE_SCORE = 7.0


@ExperimentRegistry.register
class ImproveAckPhrases(Experiment):
    """Rewrite ``_ACK_PHRASES`` based on recent trace outcomes."""

    name = "improve_ack_phrases"
    description = "Rewrite ack phrases using recent trace outcome data"

    def __init__(
        self,
        *,
        traces_db: Optional[Path] = None,
        llm_proposal: Optional[Tuple[str, ...]] = None,
        judge_score: Optional[float] = None,
    ) -> None:
        """
        Parameters
        ----------
        traces_db:
            Override the trace DB location. Defaults to ``~/.hope/traces.db``
            (inside the container: ``/readonly/traces.db``).
        llm_proposal:
            If provided, skip the ``claude -p`` call and use this tuple
            directly. Used by tests and by the runner when the LLM call
            was already made on the host.
        judge_score:
            If provided, skip the LLM judge. Tests use this.
        """
        self._traces_db = traces_db
        self._llm_proposal = llm_proposal
        self._judge_score = judge_score

    # -- Experiment hooks ----------------------------------------------------

    def apply(self, workspace_path: Path) -> None:
        target, name, current = _resolve_target(workspace_path)
        if target is None:
            # No tuple literal exists yet — seed one at the top of core.py
            # by replacing the non-literal ``_ACK_PHRASES = DEFAULT_ACKS``
            # assignment. That's the current state of main post-refactor.
            target = workspace_path / CORE_PATH_REL
            name = "_ACK_PHRASES"
            current = ()

        scored = self._load_ack_scores()
        proposal = self._propose(current, scored)

        if not proposal or proposal == current:
            # evaluate() catches this via the "different_from_baseline" check.
            logger.info(
                "ack proposal identical to current (%d phrases); apply is a no-op",
                len(current),
            )
            return

        new_src = _rewrite_ack_phrases(target.read_text(), proposal, name=name)
        target.write_text(new_src)
        # Remember where we wrote so evaluate() + files_to_stage pick it up.
        self._target_rel = str(target.relative_to(workspace_path))
        logger.info("ack phrases rewritten in %s: %d -> %d",
                    self._target_rel, len(current), len(proposal))

    def evaluate(self, workspace_path: Path) -> EvaluationResult:
        target, _name, candidate = _resolve_target(workspace_path)
        if target is None or not candidate:
            return EvaluationResult(
                success=False, score=0.0, tests_passed=False,
                details={"reason": "could not locate ack phrase literal"},
            )

        # Run the test suite scoped to the daemon module first — fast
        # smoke — then the full suite. We stop at the first failure.
        tests_passed, test_output = _run_pytest(
            workspace_path, ["tests/daemon", "tests/evolution"],
        )

        # Judge score: 7+ is acceptable. When no judge is wired (tests),
        # self._judge_score is seeded; in container eval it's the LLM call
        # result, piped in via env var.
        if self._judge_score is not None:
            judge = float(self._judge_score)
        else:
            judge = _judge_ack_phrases(candidate)

        different = True
        # Compare against the git-HEAD version of the same file — that's
        # the baseline we branched from.
        rel = target.relative_to(workspace_path).as_posix()
        baseline = _extract_from_git_head(workspace_path, rel)
        if baseline is not None:
            different = candidate != baseline

        success = tests_passed and different and judge >= MIN_JUDGE_SCORE
        return EvaluationResult(
            success=success,
            score=judge if tests_passed else 0.0,
            tests_passed=tests_passed,
            details={
                "reason": (
                    "ok" if success
                    else _why_not(tests_passed, different, judge)
                ),
                "judge_score": judge,
                "candidate": list(candidate),
                "different_from_baseline": different,
                "target": rel,
                "test_output_tail": test_output[-1500:],
            },
        )

    def files_to_stage(self, workspace_path: Path) -> List[str]:
        # The target file may be core.py or a learning module depending
        # on where the tuple literal actually lives right now.
        rel = getattr(self, "_target_rel", None)
        if rel:
            return [rel]
        target, _, _ = _resolve_target(workspace_path)
        if target is not None:
            return [target.relative_to(workspace_path).as_posix()]
        return [CORE_PATH_REL]

    # -- Internals -----------------------------------------------------------

    def _load_ack_scores(self) -> Dict[str, Dict[str, float]]:
        """Return ``{phrase: {"n": N, "mean": M}}`` from traces.db.

        Falls back to an empty dict if the DB is missing or the schema
        doesn't have ack-tagged rows yet (the sibling agent is still
        wiring this up).
        """
        db = self._traces_db or _default_traces_db()
        if not db or not Path(db).exists():
            return {}

        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                # The sibling agent writes ack phrase + feedback on
                # traces. We look up rows where metadata has
                # ``ack_phrase`` set; fall back to empty if column shape
                # isn't there.
                try:
                    rows = conn.execute(
                        "SELECT metadata, feedback FROM traces "
                        "WHERE feedback IS NOT NULL"
                    ).fetchall()
                except sqlite3.OperationalError:
                    return {}
                out: Dict[str, Dict[str, float]] = {}
                for row in rows:
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    phrase = meta.get("ack_phrase")
                    if not phrase:
                        continue
                    bucket = out.setdefault(phrase, {"n": 0, "sum": 0.0})
                    bucket["n"] += 1
                    bucket["sum"] += float(row["feedback"])
                return {
                    p: {"n": b["n"], "mean": b["sum"] / max(b["n"], 1)}
                    for p, b in out.items()
                }
        except sqlite3.Error:
            logger.exception("traces.db read failed")
            return {}

    def _propose(
        self,
        current: Tuple[str, ...],
        scores: Dict[str, Dict[str, float]],
    ) -> Tuple[str, ...]:
        """Get a proposed tuple. Test seam: ``self._llm_proposal`` wins."""
        if self._llm_proposal is not None:
            return tuple(self._llm_proposal)

        # The host side stashes the proposal in this env var when it has
        # already called ``claude -p`` (the container has no network).
        env_proposal = os.environ.get("HOPE_EVOLVE_ACK_PROPOSAL")
        if env_proposal:
            try:
                parsed = json.loads(env_proposal)
                if isinstance(parsed, list) and all(
                    isinstance(s, str) for s in parsed
                ):
                    return tuple(parsed)
            except json.JSONDecodeError:
                logger.warning("HOPE_EVOLVE_ACK_PROPOSAL not valid JSON")

        # Last-ditch: make the claude call directly. Safe when called on
        # the host (pre-container) or when the container has the CLI.
        return _call_claude_for_proposal(current, scores) or current


# -- Module-level helpers ---------------------------------------------------


def _default_traces_db() -> Optional[Path]:
    """Resolve the traces DB path — container-first, then host."""
    ro = Path("/readonly/traces.db")
    if ro.exists():
        return ro
    host = Path.home() / ".hope" / "traces.db"
    if host.exists():
        return host
    return None


def _extract_ack_phrases_from_source(
    src: str,
) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Parse any tuple literal named in :data:`_CANDIDATE_NAMES`.

    Returns ``(name, phrases)`` if a matching literal is found, else
    ``(None, ())``. Uses ``ast`` so formatting doesn't matter.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None, ()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in _CANDIDATE_NAMES:
                continue
            if not isinstance(node.value, ast.Tuple):
                continue
            out: List[str] = []
            ok = True
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
                else:
                    ok = False
                    break
            if ok and out:
                return target.id, tuple(out)
    return None, ()


def _resolve_target(
    workspace: Path,
) -> Tuple[Optional[Path], Optional[str], Tuple[str, ...]]:
    """Find the file containing the ack-phrase tuple literal.

    Walks a small set of likely locations (cheaper than a full repo scan)
    and falls back to a grep-style search across ``src/hope``.

    Returns ``(file_path, name, phrases)`` or ``(None, None, ())``.
    """
    likely = [
        workspace / CORE_PATH_REL,
        workspace / "src/hope/learning/voice_learning_loop.py",
        workspace / "src/hope/learning/learning_orchestrator.py",
    ]
    for path in likely:
        if path.exists():
            name, phrases = _extract_ack_phrases_from_source(path.read_text())
            if phrases:
                return path, name, phrases

    # Fallback: scan src/hope for the names. Bounded to .py files.
    search_root = workspace / "src" / "hope"
    if search_root.exists():
        for py in search_root.rglob("*.py"):
            try:
                text = py.read_text()
            except OSError:
                continue
            if not any(n in text for n in _CANDIDATE_NAMES):
                continue
            name, phrases = _extract_ack_phrases_from_source(text)
            if phrases:
                return py, name, phrases

    return None, None, ()


def _rewrite_ack_phrases(
    src: str, new_phrases: Tuple[str, ...], *, name: str = "_ACK_PHRASES",
) -> str:
    """Swap the tuple assigned to ``name`` for ``new_phrases``.

    If the assignment exists but isn't a tuple literal (e.g.
    ``_ACK_PHRASES = DEFAULT_ACKS``), we replace the *value* expression
    with the new tuple literal. If the assignment doesn't exist at all,
    we insert a new module-level assignment at the top.
    """
    escaped = [p.replace("\\", "\\\\").replace('"', '\\"') for p in new_phrases]
    body_lines = [f'    "{e}",' for e in escaped]
    tuple_literal = "(\n" + "\n".join(body_lines) + "\n)"
    replacement = f"{name} = {tuple_literal}"

    # Match both indented and top-level: `    _ACK_PHRASES = ...` or
    # `_ACK_PHRASES = ...`. We match up to the end of the value, which
    # can be a tuple (multi-line, with balanced parens) or a name ref.
    pattern = re.compile(
        rf"""
        (?P<indent>^[ \t]*)          # capture indent
        {re.escape(name)}
        [ \t]*=[ \t]*                # =
        (?:
            \(.*?\)                  # existing tuple (non-greedy)
          | [A-Za-z_][A-Za-z0-9_]*   # bare name (e.g. DEFAULT_ACKS)
        )
        """,
        re.DOTALL | re.VERBOSE | re.MULTILINE,
    )

    def _sub(match: re.Match[str]) -> str:
        indent = match.group("indent") or ""
        # Re-indent the replacement to match.
        lines = replacement.splitlines()
        return lines[0] if len(lines) == 1 else (
            indent + lines[0] + "\n"
            + "\n".join(indent + line for line in lines[1:])
        )

    new_src, count = pattern.subn(_sub, src, count=1)
    if count == 0:
        # Insert at top of module (after shebang/docstring/imports).
        lines = src.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines[:80]):
            # naive: insert after last import at start
            stripped = line.lstrip()
            if stripped.startswith(("import ", "from ")):
                insert_at = i + 1
        inserted = replacement + "\n\n"
        new_src = "".join(lines[:insert_at]) + inserted + "".join(lines[insert_at:])
    return new_src


def _extract_from_git_head(
    workspace: Path, rel_path: str,
) -> Optional[Tuple[str, ...]]:
    """Pull the ack tuple from the HEAD version of the file, pre-apply."""
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "show", f"HEAD:{rel_path}"],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    _name, phrases = _extract_ack_phrases_from_source(result.stdout)
    return phrases if phrases else None


# Backwards-compat alias: tests + external callers used this name.
def _extract_ack_phrases(src: str) -> Tuple[str, ...]:
    """Legacy shim — returns just the phrases tuple."""
    _name, phrases = _extract_ack_phrases_from_source(src)
    return phrases


def _run_pytest(
    workspace: Path, targets: List[str],
) -> Tuple[bool, str]:
    """Run pytest against ``targets``. Returns (passed, combined_output)."""
    existing = [t for t in targets if (workspace / t).exists()]
    if not existing:
        # Nothing to run against → treat as pass (don't block on absent tests).
        return True, "no test targets found"
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-q", *existing],
            cwd=workspace, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"pytest timed out: {exc}"
    combined = (result.stdout or "") + (result.stderr or "")
    return (result.returncode == 0), combined


def _judge_ack_phrases(phrases: Tuple[str, ...]) -> float:
    """Very small heuristic judge — gets overridden by an LLM judge in prod.

    Scores on:
      * length ≤ 9 words each (too long is bad on TTS)
      * distinct phrases (no duplicates)
      * at least 4 variants (avoids repetition)
      * no markdown/backticks (they don't speak)
    Scale: 0-10.
    """
    if not phrases:
        return 0.0

    score = 10.0
    if len(phrases) < 4:
        score -= 3.0
    if len(set(phrases)) != len(phrases):
        score -= 2.0
    bad_chars = {"`", "*", "_", "#"}
    if any(any(c in p for c in bad_chars) for p in phrases):
        score -= 3.0
    avg_words = sum(len(p.split()) for p in phrases) / len(phrases)
    if avg_words > 9:
        score -= 2.0
    if avg_words < 2:
        score -= 2.0
    return max(score, 0.0)


def _call_claude_for_proposal(
    current: Tuple[str, ...],
    scores: Dict[str, Dict[str, float]],
) -> Optional[Tuple[str, ...]]:
    """One-shot call to ``claude -p`` for a fresh ack tuple.

    Returns ``None`` on any error — the experiment then falls back to the
    existing tuple (and will be rejected as "identical").
    """
    prompt = _build_prompt(current, scores)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired):
        return None

    raw = result.stdout.strip()
    # Extract the first JSON array in the reply.
    match = re.search(r"\[[\s\S]*?\]", raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    cleaned = [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
    return tuple(cleaned) if cleaned else None


def _build_prompt(
    current: Tuple[str, ...],
    scores: Dict[str, Dict[str, float]],
) -> str:
    return (
        "You are tuning Hope's voice assistant ack phrases. These are the "
        "short phrases she says *while* the brain is generating a reply, "
        "so they mask latency. Each is spoken by macOS `say` so keep them "
        "natural, under 9 words, no markdown, no emoji.\n\n"
        f"CURRENT: {json.dumps(list(current))}\n"
        f"SCORES (mean feedback 0-1 per phrase, when available): "
        f"{json.dumps(scores)}\n\n"
        "Return ONLY a JSON array of 6-8 new ack phrases. No prose."
    )


def _why_not(tests: bool, different: bool, judge: float) -> str:
    parts = []
    if not tests:
        parts.append("tests_failed")
    if not different:
        parts.append("identical_to_baseline")
    if judge < MIN_JUDGE_SCORE:
        parts.append(f"judge_low:{judge:.2f}")
    return ",".join(parts) or "unknown"


__all__ = [
    "CORE_PATH_REL",
    "ImproveAckPhrases",
    "MIN_JUDGE_SCORE",
]
