"""3c.2 — temporal narrative tests: "what changed since X" / "where did we leave off".

Proves the derived temporal view over the photographic spine + bi-temporal graph:
additions, supersessions (old->new), and open-loop status changes are narrated
strictly after an anchor; every line carries a receipt; the same (graph, anchor)
renders a byte-identical narrative (purity); and the resume view surfaces what is
still in flight. Anchor resolution (ISO date / last-session / origin) is covered too.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from centri.db import Database
from centri.memory_graph import (
    LOOP_OPEN,
    STANCE_REJECTED,
    Decision,
    Fact,
    MemoryGraph,
    OpenLoop,
)
from centri.temporal import TemporalNarrator


@pytest.fixture
async def graph():
    tmpdir = tempfile.mkdtemp()
    db = Database(Path(tmpdir) / "state.db")
    g = MemoryGraph(db)
    await g.ensure_tables()
    yield g, db
    await db.close()


# Stable ISO anchors (lexically ordered, fixed offset).
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-03-01T00:00:00+00:00"
T2 = "2026-06-01T00:00:00+00:00"
T3 = "2026-06-10T00:00:00+00:00"
ANCHOR = "2026-05-01T00:00:00+00:00"  # between T1 and T2


class TestChangedSince:
    async def test_addition_after_anchor_is_narrated(self, graph):
        g, db = graph
        await g.add_fact(Fact(id="f1", topic="db", statement="uses SQLite",
                              source_event_id="e1", created_at=T2))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert len(nar.lines) == 1
        ln = nar.lines[0]
        assert ln.kind == "added" and ln.category == "fact"
        assert "uses SQLite" in ln.text
        assert ln.receipt == "e1"

    async def test_addition_before_anchor_is_excluded(self, graph):
        g, db = graph
        await g.add_fact(Fact(id="f1", topic="db", statement="uses SQLite",
                              source_event_id="e1", created_at=T1))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert nar.is_empty()

    async def test_supersession_renders_old_to_new_with_new_receipt(self, graph):
        g, db = graph
        # old created before anchor, superseded after -> a "changed" line.
        await g.supersede_fact(Fact(id="f1", topic="auth", statement="named authsvc",
                                    source_event_id="e1", created_at=T1))
        await g.supersede_fact(Fact(id="f2", topic="auth", statement="renamed to identity",
                                    source_event_id="e2", created_at=T2))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        lines = [ln for ln in nar.lines if ln.category == "fact"]
        assert len(lines) == 1
        ln = lines[0]
        assert ln.kind == "superseded"
        assert "authsvc" in ln.text and "identity" in ln.text
        # Receipt points at the NEW (live) value, the verifiable current ground truth.
        assert ln.receipt == "e2"

    async def test_rejection_decision_is_categorized(self, graph):
        g, db = graph
        await g.add_decision(Decision(id="d1", topic="orm", statement="use raw SQL",
                                      stance=STANCE_REJECTED, source_event_id="e1",
                                      created_at=T2))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert nar.lines[0].category == "rejection"
        assert "rejected" in nar.lines[0].text

    async def test_open_loop_states_change_after_anchor(self, graph):
        g, db = graph
        # new loop opened after anchor
        await g.add_open_loop(OpenLoop(id="l1", intent="wire voice input",
                                       source_event_id="e1", created_at=T2,
                                       updated_at=T2, last_touched_at=T2))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        loop_lines = [ln for ln in nar.lines if ln.category == "open_loop"]
        assert len(loop_lines) == 1
        assert loop_lines[0].kind == "added"
        assert "wire voice input" in loop_lines[0].text

    async def test_reserved_ambient_fact_is_excluded(self, graph):
        g, db = graph
        await g.add_fact(Fact(id="amb", topic="ambient-standing-context",
                              statement="standing digest", source_event_id="e1",
                              created_at=T2))
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert nar.is_empty()

    async def test_narrative_is_deterministic_byte_identical(self, graph):
        g, db = graph
        await g.add_fact(Fact(id="f1", topic="db", statement="uses SQLite",
                              source_event_id="e1", created_at=T2))
        await g.add_decision(Decision(id="d1", topic="orm", statement="use raw SQL",
                                      source_event_id="e2", created_at=T3))
        n1 = await TemporalNarrator(g, db).changed_since(ANCHOR)
        n2 = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert n1.render() == n2.render()
        # Newest change first (T3 decision before T2 fact).
        assert n1.lines[0].category == "decision"

    async def test_render_header_names_the_anchor(self, graph):
        g, db = graph
        nar = await TemporalNarrator(g, db).changed_since(ANCHOR)
        assert nar.render().startswith(f"What changed since {ANCHOR}:")


class TestWhereLeftOff:
    async def test_surfaces_open_loops_and_last_decision(self, graph):
        g, db = graph
        await g.add_open_loop(OpenLoop(id="l1", intent="finish the bench",
                                       state=LOOP_OPEN, source_event_id="e1",
                                       created_at=T1, updated_at=T1, last_touched_at=T1))
        await g.add_decision(Decision(id="d1", topic="orm", statement="use raw SQL",
                                      source_event_id="e2", created_at=T2))
        await db.append_event(event_id="ev9", type="user.utterance", source="chat",
                              ts=T3, payload={"text": "let's resume tomorrow"})
        nar = await TemporalNarrator(g, db).where_left_off()
        kinds = {ln.kind for ln in nar.lines}
        assert "in_flight" in kinds  # open loop still open
        assert "last" in kinds       # decision + last event
        assert any("finish the bench" in ln.text for ln in nar.lines)
        assert any(ln.category == "event" for ln in nar.lines)
        assert nar.render().startswith("Where we left off:")

    async def test_every_line_has_a_receipt(self, graph):
        g, db = graph
        await g.add_open_loop(OpenLoop(id="l1", intent="finish the bench",
                                       state=LOOP_OPEN, source_event_id="e1",
                                       created_at=T1, updated_at=T1, last_touched_at=T1))
        await db.append_event(event_id="ev9", type="user.utterance", source="chat",
                              ts=T3, payload={"text": "hi"})
        nar = await TemporalNarrator(g, db).where_left_off()
        assert all(ln.receipt for ln in nar.lines)

    async def test_skips_derived_bookkeeping_for_last_activity(self, graph):
        g, db = graph
        await db.append_event(event_id="ev1", type="user.utterance", source="chat",
                              ts=T2, payload={"text": "real work"})
        await db.append_event(event_id="ev2", type="curation.brief", source="curator",
                              ts=T3, payload={"lines": []})
        nar = await TemporalNarrator(g, db).where_left_off()
        evt = [ln for ln in nar.lines if ln.category == "event"]
        assert evt and evt[0].receipt == "ev1"  # not the curation.brief


class TestAnchorResolution:
    async def test_bare_date_anchors_at_start_of_day(self, graph):
        g, db = graph
        out = await TemporalNarrator(g, db).resolve_anchor("2026-06-10")
        assert out == {"anchor": "2026-06-10T00:00:00+00:00", "kind": "iso"}

    async def test_empty_is_origin(self, graph):
        g, db = graph
        out = await TemporalNarrator(g, db).resolve_anchor("")
        assert out["kind"] == "origin" and out["anchor"] == ""

    async def test_full_iso_passes_through(self, graph):
        g, db = graph
        out = await TemporalNarrator(g, db).resolve_anchor(T2)
        assert out == {"anchor": T2, "kind": "iso"}

    async def test_last_session_finds_the_idle_gap(self, graph):
        g, db = graph
        # two clustered events, a big gap, then the current session.
        await db.append_event(event_id="e1", type="user.utterance", source="chat",
                              ts="2026-06-01T09:00:00+00:00", payload={})
        await db.append_event(event_id="e2", type="user.utterance", source="chat",
                              ts="2026-06-01T09:05:00+00:00", payload={})
        await db.append_event(event_id="e3", type="user.utterance", source="chat",
                              ts="2026-06-10T10:00:00+00:00", payload={})
        out = await TemporalNarrator(g, db).resolve_anchor("last-session")
        assert out["kind"] == "last-session"
        # Anchor = the older side of the gap (end of the previous session).
        assert out["anchor"] == "2026-06-01T09:05:00+00:00"

    async def test_last_session_falls_back_to_origin_without_a_gap(self, graph):
        g, db = graph
        await db.append_event(event_id="e1", type="user.utterance", source="chat",
                              ts="2026-06-10T10:00:00+00:00", payload={})
        out = await TemporalNarrator(g, db).resolve_anchor("last-session")
        assert out["kind"] == "origin" and out["anchor"] == ""


class TestIntentMatchers:
    """Pure, deterministic temporal-intent detection used by the coordinator."""

    def test_resume_phrases_match(self):
        from centri.coordinator import _is_resume_query, _is_temporal_query
        for p in ["where did we leave off", "catch me up", "where were we"]:
            assert _is_resume_query(p)
            assert _is_temporal_query(p)

    def test_since_phrases_are_temporal_not_resume(self):
        from centri.coordinator import _is_resume_query, _is_temporal_query
        p = "what changed since 2026-06-01"
        assert _is_temporal_query(p)
        assert not _is_resume_query(p)

    def test_plain_coding_request_is_not_temporal(self):
        from centri.coordinator import _is_temporal_query
        assert not _is_temporal_query("add a new endpoint and write tests")

    def test_extract_since_reads_iso_date_session_or_origin(self):
        from centri.coordinator import _extract_since
        assert _extract_since("what changed since 2026-06-10") == "2026-06-10"
        assert _extract_since("what changed since last session") == "last-session"
        assert _extract_since("what's new") == ""
