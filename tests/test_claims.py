"""Stage 2: one assessment per claim, using all of the claim's sources together."""

import pytest
from conftest import FIXTURES, FakeFetcher, FakeJudge, judgement
from sqlalchemy import select

from wcm import pipeline
from wcm.models import Claim, Finding, Review, Verification
from wcm.pipeline import Analyzer
from wcm.wikipedia import RevisionInfo
from wcm.worker import enqueue

A, B, D = "https://a.example/tower", "https://b.example/paint", "https://d.example/other"
PAGES = {
    A: "The tower is 60 metres tall. The keeper's house was built in 1880.",
    B: "It was painted red in 1902. The station closed in 1990.",
    D: "A general history of the coast.",
}


class MultiWiki:
    lang = "en"

    def revision_info(self, title=None, rev_id=None):
        return RevisionInfo(title="Example Point Light", rev_id=2000, timestamp=None)

    def revision_html(self, rev_id):
        return (FIXTURES / "parsoid_multi.html").read_text()


def q(sources, phrase):
    """A quote of `phrase` from whichever source contains it."""
    for s in sources:
        if phrase in s.text:
            return (s.source_id, phrase)
    raise AssertionError(f"{phrase!r} not in any source")


def has(sources, phrase):
    return any(phrase in s.text for s in sources)


def rule(claim, sources):
    t, cid = claim.text, claim.claim_id
    if t.startswith("The tower is 60"):
        if has(sources, "60 metres tall") and has(sources, "painted red in 1902"):
            return judgement(
                cid,
                "supported",
                supporting=[q(sources, "60 metres tall"), q(sources, "painted red in 1902")],
            )
        if has(sources, "60 metres tall"):
            return judgement(
                cid,
                "partially_supported",
                supporting=[q(sources, "60 metres tall")],
                parts=["painted red"],
            )
        return judgement(
            cid,
            "partially_supported",
            supporting=[q(sources, "painted red in 1902")],
            parts=["60 metres"],
        )
    if t.startswith("The keeper's house"):
        return judgement(
            cid,
            "partially_supported",
            supporting=[q(sources, "built in 1880")],
            parts=["burned in 1925"],
        )
    if t.startswith("The light was visible"):
        return judgement(cid, "not_supported")
    if t.startswith("The station closed"):
        if has(sources, "closed in 1990"):
            return judgement(cid, "supported", supporting=[q(sources, "closed in 1990")])
        return judgement(cid, "not_supported")
    if t.startswith("Its height is 61"):
        return judgement(
            cid,
            "partially_supported",
            supporting=[q(sources, "60 metres tall")],
            parts=["61 metres"],
            severity="minor",
        )
    raise AssertionError(t)


def run(session, judge=None):
    judge = judge or FakeJudge(rule)
    r = enqueue(session, "Example Point Light")
    Analyzer(session, MultiWiki(), FakeFetcher(PAGES), judge).run(r)
    return r, judge


def finding_for(session, prefix):
    claim = session.scalars(select(Claim).where(Claim.text.startswith(prefix))).one()
    return [f for f in session.scalars(select(Finding).where(Finding.claim_id == claim.id))]


def test_sources_that_each_cover_part_are_judged_together(session):
    r, judge = run(session)
    assert r.status == "done", r.error
    assert finding_for(session, "The tower is 60") == []
    assert ((A, B), ["The tower is 60 metres tall and was painted red in 1902."]) in judge.calls
    combined = session.scalars(select(Verification).where(Verification.kind == "claim")).all()
    tower = next(v for v in combined if v.claim.text.startswith("The tower"))
    assert tower.verdict == "supported"
    # Each quote records which citation it came from.
    by_text = {qt["text"]: qt["citation_id"] for qt in tower.quotes}
    assert by_text["60 metres tall"] != by_text["painted red in 1902"]
    assert all(qt["verified"] for qt in tower.quotes)


def test_one_finding_per_claim_even_with_two_failing_sources(session):
    run(session)
    [f] = finding_for(session, "The light was visible")
    assert f.verification.kind == "claim"
    assert f.verification.verdict == "not_supported"


def test_offline_source_makes_it_a_low_priority_check(session):
    run(session)
    [f] = finding_for(session, "The keeper's house")
    assert f.needs_offline_check
    assert f.priority == "low"


def test_any_supporting_source_is_enough(session):
    _, judge = run(session)
    assert finding_for(session, "The station closed") == []
    assert not any(isinstance(c[0], tuple) and D in c[0] for c in judge.calls)


def test_minor_differences_are_low_priority(session):
    run(session)
    [f] = finding_for(session, "Its height is 61")
    assert f.priority == "low"
    assert f.verification.severity == "minor"


def test_totals(session):
    r, _ = run(session)
    assert r.stats["claims"] == 5
    assert r.stats["claims_assessed"] == 5
    assert r.stats["findings"] == 3
    assert r.stats["needs_offline_check"] == 1
    assert r.stats["claims_supported"] == 2


@pytest.fixture
def old_prompt(monkeypatch):
    def use(version):
        monkeypatch.setattr(pipeline, "PROMPT_VERSION", version)

    return use


def test_new_prompt_supersedes_undecided_findings_but_keeps_human_decisions(session, old_prompt):
    # First run under an "older prompt" that judged every claim not supported.
    old_prompt("judge-v1")
    run(session, FakeJudge(lambda c, s: judgement(c.claim_id, "not_supported")))
    session.commit()
    old = {f.claim.text[:20]: f for f in session.scalars(select(Finding))}
    assert len(old) == 5
    # A person confirmed one and dismissed another; the rest are undecided.
    old["The light was visibl"].status = "confirmed"
    session.add(
        Review(
            finding=old["The light was visibl"],
            reviewer="alice",
            decision="confirmed",
            note="checked",
        )
    )
    old["The station closed i"].status = "dismissed"
    session.commit()

    old_prompt("judge-v2")
    r, _ = run(session)
    statuses = {
        f.claim.text[:20]: f.status
        for f in session.scalars(select(Finding))
        if f.verification.prompt_version == "judge-v1"
    }
    assert statuses["The light was visibl"] == "confirmed"
    assert statuses["The station closed i"] == "dismissed"
    assert statuses["The tower is 60 metr"] == "superseded"
    assert statuses["The keeper's house w"] == "superseded"
    # New findings only where the claim still fails and nobody has decided yet.
    new = {
        f.claim.text[:20]
        for f in session.scalars(select(Finding))
        if f.verification.prompt_version == "judge-v2"
    }
    assert new == {"The keeper's house w", "Its height is 61 met"}
    assert r.stats["superseded_findings"] == 3
