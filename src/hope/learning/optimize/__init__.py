"""Optimization surface for Hope's self-learning loop.

The academic benchmark harness (optimizer.py, trial_runner.py,
search_space.py, llm_optimizer, store, types) was removed when Hope
pivoted from an in-process inference engine to the tmux-brain. What
remains here is the narrow surface the voice-turn learning loop uses,
owned by the sibling learning agent:

* :mod:`hope.learning.optimize.feedback` — FeedbackCollector, TraceJudge
* :mod:`hope.learning.optimize.personal` — personal synthesizer
"""

from __future__ import annotations

# Lazy re-exports so a broken submodule (e.g. the sibling branch still
# references the deleted hope.evals backend) doesn't block the package
# from importing.
try:
    from hope.learning.optimize.feedback.collector import FeedbackCollector
    from hope.learning.optimize.feedback.judge import TraceJudge

    __all__ = ["FeedbackCollector", "TraceJudge"]
except ImportError:
    __all__ = []
