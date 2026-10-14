"""Seed the database with a synthetic article and canned verdicts, so the review UI
can be tried without network access or an API key. Nothing here touches Wikipedia
or Claude."""

from pathlib import Path

from sqlalchemy.orm import Session

from wcm.judge import ClaimJudgement, JudgeResult, Quote
from wcm.pipeline import Analyzer
from wcm.sources import FetchResult
from wcm.wikipedia import RevisionInfo
from wcm.worker import enqueue

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "parsoid_sample.html"

LIVE = "https://example.org/lighthouses/harbor-point"
ARCHIVE = "https://web.archive.org/web/20200101000000/https://example.org/lighthouses/harbor-point"
NEWS = "https://news.example.com/1998/history"
PAGES = {
    LIVE: "Harbor Point Light was completed in 1871 and first lit that May. "
    "The light was automated in 1972, after the last keeper retired.",
    "archive:" + ARCHIVE: "Harbor Point Light was completed in 1871 and first lit that May. "
    "The light was automated in 1972, after the last keeper retired.",
    NEWS: "The Board approved the station in 1868. Its engineer, Alfred Smith, drew up "
    "plans for the tower, while the lantern room was ordered from a Paris workshop.",
}


class _Wiki:
    lang = "en"

    def revision_info(self, title=None, rev_id=None):
        return RevisionInfo(title="Harbor Point Lighthouse (demo)", rev_id=1000, timestamp=None)

    def revision_html(self, rev_id):
        return FIXTURE.read_text()


class _Fetcher:
    def fetch(self, url, via_archive=False):
        text = PAGES.get(("archive:" if via_archive else "") + url)
        if text is None:
            return FetchResult(url, "http_error", http_status=404, via_archive=via_archive)
        return FetchResult(url, "ok", text=text, final_url=url, via_archive=via_archive,
                           http_status=200, title="Demo source")

    def find_archive(self, url, near):
        return None


def _j(cid, verdict, supporting=(), contradicting=(), parts=(), explanation="", conf=0.85):
    return ClaimJudgement(
        claim_id=cid, verdict=verdict,
        supporting_quotes=[Quote(text=t, translation="") for t in supporting],
        contradicting_quotes=[Quote(text=t, translation="") for t in contradicting],
        unsupported_parts=list(parts), explanation=explanation, confidence=conf,
    )


class _Judge:
    def judge(self, *, citation_text, source_title, source_url, source_text, claims):
        out = {}
        for c in claims:
            if c.text.startswith("It was completed"):
                out[c.claim_id] = _j(c.claim_id, "supported",
                                     supporting=["completed in 1871 and first lit that May"],
                                     explanation="The source gives the same year and month.")
            elif c.text.startswith("The light was automated"):
                out[c.claim_id] = _j(
                    c.claim_id, "contradicted", contradicting=["automated in 1972"],
                    parts=["automated in 1965", "keeper's house became a museum"],
                    explanation="The source dates automation to 1972, not 1965, and does not "
                    "mention the museum.", conf=0.9)
            else:
                out[c.claim_id] = _j(
                    c.claim_id, "partially_supported",
                    supporting=["Its engineer, Alfred Smith, drew up plans for the tower"],
                    contradicting=["the lantern room was ordered from a Paris workshop"],
                    parts=["designed the lantern room"],
                    explanation="The source names Smith as the engineer who planned the tower, "
                    "but says the lantern room was ordered from Paris.", conf=0.7)
        return JudgeResult(judgements=out, model="demo", served_model="demo (canned)")


def seed(session: Session) -> int:
    run = enqueue(session, "Harbor Point Lighthouse (demo)", requested_by="demo")
    Analyzer(session, _Wiki(), _Fetcher(), _Judge()).run(run)
    return run.id
