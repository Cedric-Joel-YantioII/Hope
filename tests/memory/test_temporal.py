"""Temporal-fact RAG layer — round-trip + supersedence tests."""

from __future__ import annotations

import os
import tempfile

import pytest

# The Rust SQLiteMemory pulls in heavy native deps that may not be
# installed in every test environment. Skip cleanly if it's missing.
pytest.importorskip("hope.tools.storage.sqlite")


@pytest.fixture
def backend():
    from hope.tools.storage.sqlite import SQLiteMemory

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield SQLiteMemory(db_path=path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_store_and_current(backend):
    from hope.memory.temporal import current_facts, store_fact

    store_fact("alex-chen", "Alex Chen — Senior Recruiter at Stripe.",
               valid_from="2023-01-01T00:00:00",
               valid_to="2024-09-01T00:00:00", backend=backend)
    store_fact("alex-chen", "Alex Chen — Recruiting Lead at Anthropic.",
               valid_from="2024-09-01T00:00:00", backend=backend)

    now = current_facts("alex-chen", backend=backend)
    assert len(now) == 1, f"expected 1 current fact, got {now}"
    assert "Anthropic" in now[0].content

    past = current_facts("alex-chen",
                         as_of="2024-01-15T00:00:00", backend=backend)
    assert len(past) == 1
    assert "Stripe" in past[0].content


def test_history_returns_all(backend):
    from hope.memory.temporal import fact_history, store_fact

    store_fact("e", "fact A", valid_from="2023-01-01T00:00:00",
               valid_to="2023-06-01T00:00:00", backend=backend)
    store_fact("e", "fact B", valid_from="2023-06-01T00:00:00",
               backend=backend)

    hist = fact_history("e", backend=backend)
    assert len(hist) == 2
    # Most recent valid_from first.
    assert hist[0].content == "fact B"
    assert hist[1].content == "fact A"


def test_supersede_marks_old_invalid(backend):
    from hope.memory.temporal import current_facts, store_fact, supersede_fact

    old = store_fact("p", "phone +1 555 OLD", backend=backend)
    new = supersede_fact(old, "phone +1 555 NEW", backend=backend)
    assert old != new

    now = current_facts("p", backend=backend)
    assert len(now) == 1
    assert now[0].content == "phone +1 555 NEW"
    assert now[0].doc_id == new


def test_entity_disambiguation(backend):
    from hope.memory.temporal import current_facts, store_fact

    store_fact("alex-chen-stripe", "Alex Chen — works at Stripe.",
               backend=backend)
    store_fact("alex-rivera-anthropic",
               "Alex Rivera — works at Anthropic.", backend=backend)

    a = current_facts("alex-chen-stripe", backend=backend)
    b = current_facts("alex-rivera-anthropic", backend=backend)
    assert len(a) == 1 and "Stripe" in a[0].content
    assert len(b) == 1 and "Anthropic" in b[0].content
    # No cross-pollution.
    assert a[0].entity != b[0].entity
