"""Seed the database with a synthetic article and canned verdicts, so the review UI
can be tried without network access or an API key. Nothing here touches Wikipedia
or Claude."""

from pathlib import Path

from sqlalchemy.orm import Session

from wcm.judge import ClaimJudgement, JudgeResult, Quote
from wcm.pipeline import Analyzer
from wcm.sources import FetchResult
from wcm.triage import StatementTriage, TriageResult
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
        return FetchResult(
            url,
            "ok",
            text=text,
            final_url=url,
            via_archive=via_archive,
            http_status=200,
            title="Demo source",
        )

    def find_archive(self, url, near):
        return None


def _j(cid, verdict, supporting=(), contradicting=(), parts=(), explanation="", conf=0.85):
    return ClaimJudgement(
        claim_id=cid,
        verdict=verdict,
        supporting_quotes=[Quote(source_id=1, text=t, translation="") for t in supporting],
        contradicting_quotes=[Quote(source_id=1, text=t, translation="") for t in contradicting],
        severity="none" if verdict == "supported" else "major",
        unsupported_parts=list(parts),
        explanation=explanation,
        confidence=conf,
    )


class _Judge:
    def judge(self, *, sources, claims):
        out = {}
        for c in claims:
            if c.text.startswith("It was completed"):
                out[c.claim_id] = _j(
                    c.claim_id,
                    "supported",
                    supporting=["completed in 1871 and first lit that May"],
                    explanation="The source gives the same year and month.",
                )
            elif c.text.startswith("The light was automated"):
                out[c.claim_id] = _j(
                    c.claim_id,
                    "contradicted",
                    contradicting=["automated in 1972"],
                    parts=["automated in 1965", "keeper's house became a museum"],
                    explanation="The source dates automation to 1972, not 1965, and does not "
                    "mention the museum.",
                    conf=0.9,
                )
            else:
                out[c.claim_id] = _j(
                    c.claim_id,
                    "partially_supported",
                    supporting=["Its engineer, Alfred Smith, drew up plans for the tower"],
                    contradicting=["the lantern room was ordered from a Paris workshop"],
                    parts=["designed the lantern room"],
                    explanation="The source names Smith as the engineer who planned the tower, "
                    "but says the lantern room was ordered from Paris.",
                    conf=0.7,
                )
        return JudgeResult(judgements=out, model="demo", served_model="demo (canned)")


class _TownWiki:
    lang = "en"

    def revision_info(self, title=None, rev_id=None):
        return RevisionInfo(title="Example Town (demo)", rev_id=1001, timestamp=None)

    def revision_html(self, rev_id):
        return (FIXTURE.parent / "parsoid_uncited.html").read_text()


class _Triage:
    """Canned rankings for the demo's uncited statements."""

    def triage(self, *, article_title, statements, cited_elsewhere=()):
        out = {}
        for st in statements:
            t = st.text
            census = next((c.statement_id for c in cited_elsewhere if "4,200" in c.text), None)
            if "4,200" in t:
                r = StatementTriage(
                    statement_id=st.statement_id,
                    category="specific",
                    living_person=False,
                    reason="population figure",
                    covered_by=census,
                )
            elif t.startswith('"'):
                r = StatementTriage(
                    statement_id=st.statement_id,
                    category="quotation",
                    living_person=False,
                    reason="quotation",
                )
            elif "killed" in t:
                r = StatementTriage(
                    statement_id=st.statement_id,
                    category="contentious",
                    living_person=False,
                    reason="casualty figure",
                )
            elif "accused" in t:
                r = StatementTriage(
                    statement_id=st.statement_id,
                    category="contentious",
                    living_person=True,
                    reason="allegation of wrongdoing against a named person",
                )
            elif "since" in t:
                r = StatementTriage(
                    statement_id=st.statement_id,
                    category="specific",
                    living_person=False,
                    reason="date",
                )
            else:
                r = StatementTriage(
                    statement_id=st.statement_id, category="general", living_person=False, reason=""
                )
            out[st.statement_id] = r
        return TriageResult(statements=out, model="demo", served_model="demo (canned)")


class _TownJudge:
    def judge(self, *, sources, claims):
        return JudgeResult(
            judgements={
                c.claim_id: _j(
                    c.claim_id,
                    "supported",
                    supporting=["4,200 residents" if "4,200" in c.text else "charter in 1851"],
                )
                for c in claims
            },
            model="demo",
            served_model="demo (canned)",
        )


TOWN_PAGES = {
    "https://town.example/history": "The town received its charter in 1851. "
    "In 2010 the town had 4,200 residents."
}


class _TownFetcher(_Fetcher):
    def fetch(self, url, via_archive=False):
        if url in TOWN_PAGES and not via_archive:
            return FetchResult(url, "ok", text=TOWN_PAGES[url], final_url=url, http_status=200)
        return super().fetch(url, via_archive)


def seed(session: Session) -> int:
    run = enqueue(session, "Harbor Point Lighthouse (demo)", requested_by="demo")
    Analyzer(session, _Wiki(), _Fetcher(), _Judge()).run(run)
    town = enqueue(session, "Example Town (demo)", requested_by="demo")
    Analyzer(session, _TownWiki(), _TownFetcher(), _TownJudge(), triage=_Triage()).run(town)
    return run.id
