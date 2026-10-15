from conftest import FakeFetcher, FakeJudge, FakeWiki, judgement
from sqlalchemy import select

from wcm.models import Finding, Verdict, Verification
from wcm.pipeline import Analyzer
from wcm.worker import enqueue

LIVE = "https://example.org/lighthouses/harbor-point"
ARCHIVE = "https://web.archive.org/web/20200101000000/https://example.org/lighthouses/harbor-point"
NEWS = "https://news.example.com/1998/history"

PAGES = {
    LIVE: "Harbor Point Light was completed in 1871. The light was automated in 1972.",
    "archive:" + ARCHIVE: "Harbor Point Light was completed in 1871. It was automated in 1972.",
    NEWS: "The Board approved the station in 1868. The lantern was imported from France.",
}


def rule(claim, source):
    if claim.text.startswith("It was completed"):
        return judgement(claim.claim_id, "supported", supporting=["completed in 1871"])
    if claim.text.startswith("The light was automated"):
        quote = "automated in 1972" if "automated in 1972" in source else "x"
        return judgement(claim.claim_id, "contradicted", contradicting=[quote])
    if claim.text.startswith("Dr. A. Smith"):
        # A quote that isn't in the source: the verdict must be thrown out.
        return judgement(
            claim.claim_id,
            "partially_supported",
            supporting=["A. Smith designed the lantern"],
            parts=["designer"],
        )
    raise AssertionError(f"unexpected claim {claim.text}")


def run_pipeline(session, fetcher=None, judge=None, recheck=False):
    fetcher = fetcher or FakeFetcher(PAGES)
    judge = judge or FakeJudge(rule)
    run = enqueue(session, "Harbor Point Lighthouse")
    Analyzer(session, FakeWiki(), fetcher, judge, recheck=recheck).run(run)
    session.commit()
    return run, fetcher, judge


def test_full_run(session):
    run, _fetcher, _judge = run_pipeline(session)
    assert run.status == "done", run.error
    s = run.stats
    assert s["claims"] == 5
    assert s["findings"] == 1
    assert s["archive_rechecks"] == 1
    assert s["rejected_unverified_quotes"] == 1
    # Book with no URL + PDF that 404s with no archive.
    assert s[f"verdict:{Verdict.source_unavailable}"] == 2

    finding = session.scalars(select(Finding)).one()
    v = finding.verification
    assert v.verdict == "contradicted"
    assert finding.priority == "high"
    # The finding rests on the archived copy, not the live page.
    assert v.snapshot.via_archive
    assert v.quotes[0]["verified"] and "automated in 1972" in v.quotes[0]["excerpt"]
    assert v.prompt_version == "judge-v1"


def test_negative_verdict_rechecked_against_archive(session):
    _, fetcher, judge = run_pipeline(session)
    assert (ARCHIVE, True) in fetcher.calls
    # Only the negative claim is re-judged on the archive.
    archive_calls = [c for c in judge.calls if c[0] == ARCHIVE]
    assert archive_calls == [
        (ARCHIVE, ["The light was automated in 1965, and the keeper's house became a museum."])
    ]


def test_rerun_skips_judged_citations_and_retries_unavailable_ones(session):
    run_pipeline(session)
    run2, _, judge2 = run_pipeline(session)
    # The 2 judged citations are skipped (no API cost); the 2 unavailable ones
    # (book without URL, dead PDF) are tried again.
    assert run2.stats["skipped_already_checked"] == 2
    assert judge2.calls == []
    assert run2.stats.get("findings", 0) == 0
    assert len(session.scalars(select(Finding)).all()) == 1
    assert len(session.scalars(select(Verification)).all()) == 5 + 2


def test_recheck_judges_everything_again_without_duplicate_findings(session):
    run_pipeline(session)
    run2, _, judge2 = run_pipeline(session, recheck=True)
    assert "skipped_already_checked" not in run2.stats
    assert judge2.calls
    assert run2.stats.get("findings", 0) == 0
    assert len(session.scalars(select(Finding)).all()) == 1
    assert len(session.scalars(select(Verification)).all()) == 2 * 5


class ExplodingFetcher(FakeFetcher):
    """Raises an unexpected error for one URL, like a corrupt PDF did in practice."""

    def __init__(self, pages, bad_url, exc):
        super().__init__(pages)
        self.bad_url, self.exc = bad_url, exc

    def fetch(self, url, via_archive=False):
        if url == self.bad_url:
            raise self.exc
        return super().fetch(url, via_archive)


def test_one_broken_source_does_not_abort_the_run(session):
    fetcher = ExplodingFetcher(PAGES, NEWS, RuntimeError("Invalid object in /Pages"))
    run, _, _ = run_pipeline(session, fetcher=fetcher)
    assert run.status == "done"
    assert run.stats["citation_errors"] == 1
    assert "Invalid object in /Pages" in run.error
    # Everything else was still checked and saved.
    assert run.stats["findings"] == 1
    assert run.stats["verdict:source_unavailable"] == 2
    assert len(session.scalars(select(Verification)).all()) == 4


def test_auth_errors_still_stop_the_run(session):
    import anthropic
    import httpx2

    err = anthropic.AuthenticationError(
        "invalid x-api-key",
        response=httpx2.Response(401, request=httpx2.Request("POST", "https://x")),
        body=None,
    )

    class BadKeyJudge(FakeJudge):
        def judge(self, **kw):
            raise err

    run, _, _ = run_pipeline(session, judge=BadKeyJudge(rule))
    assert run.status == "failed"
    assert "AuthenticationError" in run.error


def test_live_page_changed_archive_supports(session):
    pages = dict(PAGES)
    pages["archive:" + ARCHIVE] = "Completed in 1871, the light was automated in 1965."

    def r(claim, source):
        if claim.text.startswith("The light was automated") and "1965" in source:
            return judgement(claim.claim_id, "supported", supporting=["automated in 1965"])
        return rule(claim, source)

    run, _, _ = run_pipeline(session, FakeFetcher(pages), FakeJudge(r))
    assert run.stats.get("findings", 0) == 0
