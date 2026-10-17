from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, object_session, selectinload

from wcm.config import get_settings
from wcm.costs import cost_per_citation, run_cost
from wcm.db import session_factory
from wcm.discovery import RANKING_VERSION, ClaudeRanker, DiscoveryResult, discover
from wcm.judge import JudgeConfigError, JudgeError
from wcm.models import (
    AnalysisRun,
    Article,
    Collection,
    CollectionArticle,
    Finding,
    FindingStatus,
    Review,
    RunStatus,
    Verification,
)
from wcm.pipeline import latest_source_verdicts
from wcm.reports import DraftNotAllowed, talk_page_draft
from wcm.wikipedia import WikipediaClient, WikipediaError, permalink
from wcm.worker import enqueue

app = FastAPI(title="Wiki Citation Monitor", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_reviewer(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    reviewers = get_settings().reviewers()
    if creds is None or creds.credentials not in reviewers:
        raise HTTPException(401, "Invalid or missing reviewer token")
    return reviewers[creds.credentials]


DB = Annotated[Session, Depends(get_session)]
Reviewer = Annotated[str, Depends(current_reviewer)]


# --- Schemas ----------------------------------------------------------------------


class RunIn(BaseModel):
    title: str | None = None
    rev_id: int | None = None
    lang: str = Field(default="en", pattern=r"^[a-z\-]{2,12}$")


class RunOut(BaseModel):
    id: int
    lang: str
    title: str
    requested_rev_id: int | None
    rev_id: int | None
    status: str
    error: str | None
    stats: dict
    requested_by: str | None
    created_at: datetime
    finished_at: datetime | None
    # What the Claude calls in this run cost, from stored token usage.
    cost_usd: float
    collection_id: int | None


class FindingSummary(BaseModel):
    id: int
    # "F2": the sources don't support the claim. "uncited": no citation at all.
    category: str
    severity: str | None
    needs_offline_check: bool
    article: str
    lang: str
    rev_id: int
    section: str
    claim: str
    verdict: str
    priority: str
    status: str
    confidence: float | None
    created_at: datetime


class ReviewOut(BaseModel):
    reviewer: str
    decision: str
    note: str
    created_at: datetime


class SourceOut(BaseModel):
    """One of the claim's citations, and what checking its source found."""

    number: int
    citation_id: int
    citation_text: str
    citation_urls: list[str]
    citation_archive_url: str | None
    citation_quote: str | None
    verdict: str | None
    explanation: str
    source_url: str | None
    source_via_archive: bool
    source_status: str | None
    source_fetched_at: datetime | None


class FindingDetail(FindingSummary):
    permalink: str
    context_before: str
    # "source": judged against its one readable source. "claim": all of the
    # claim's readable sources were judged together. "triage": uncited statement.
    assessment: str
    # For uncited statements: living_person, in_lead, citation_needed_tag.
    details: dict
    explanation: str
    unsupported_parts: list[str]
    # Each quote has source_number, matching SourceOut.number.
    quotes: list[dict]
    sources: list[SourceOut]
    model: str | None
    served_model: str | None
    prompt_version: str | None
    reviews: list[ReviewOut]


class ReviewIn(BaseModel):
    decision: Literal["confirmed", "dismissed", "needs_research"]
    note: str = ""


# --- Helpers ----------------------------------------------------------------------


def _run_out(r: AnalysisRun) -> RunOut:
    session = object_session(r)
    return RunOut(
        cost_usd=run_cost(session, r.id) if session else 0.0,
        collection_id=r.collection_id,
        id=r.id,
        lang=r.lang,
        title=r.title,
        requested_rev_id=r.requested_rev_id,
        rev_id=r.revision.rev_id if r.revision else None,
        status=r.status,
        error=r.error,
        stats=r.stats or {},
        requested_by=r.requested_by,
        created_at=r.created_at,
        finished_at=r.finished_at,
    )


def _summary(f: Finding) -> dict:
    v = f.verification
    return {
        "id": f.id,
        "category": f.category,
        "severity": v.severity,
        "needs_offline_check": f.needs_offline_check,
        "article": f.article.title,
        "lang": f.article.lang,
        "rev_id": v.claim.revision.rev_id,
        "section": v.claim.section,
        "claim": v.claim.text,
        "verdict": v.verdict,
        "priority": f.priority,
        "status": f.status,
        "confidence": v.model_confidence,
        "created_at": f.created_at,
    }


def _finding_query():
    return select(Finding).options(
        selectinload(Finding.article),
        selectinload(Finding.reviews),
        selectinload(Finding.verification).selectinload(Verification.claim),
        selectinload(Finding.verification).selectinload(Verification.citation),
        selectinload(Finding.verification).selectinload(Verification.snapshot),
    )


def _get_finding(session: Session, finding_id: int) -> Finding:
    f = session.scalar(_finding_query().where(Finding.id == finding_id))
    if f is None:
        raise HTTPException(404, "Finding not found")
    return f


# --- Routes -----------------------------------------------------------------------


@app.get("/api/me")
def me(reviewer: Reviewer) -> dict:
    return {"reviewer": reviewer}


@app.post("/api/runs", status_code=201)
def create_run(body: RunIn, session: DB, reviewer: Reviewer) -> RunOut:
    if not body.title and body.rev_id is None:
        raise HTTPException(422, "Give an article title or a revision id")
    run = enqueue(session, body.title, body.rev_id, body.lang, requested_by=reviewer)
    return _run_out(run)


@app.get("/api/runs")
def list_runs(session: DB, _: Reviewer, limit: int = Query(50, le=200)) -> list[RunOut]:
    runs = session.scalars(
        select(AnalysisRun)
        .options(selectinload(AnalysisRun.revision))
        .order_by(AnalysisRun.id.desc())
        .limit(limit)
    )
    return [_run_out(r) for r in runs]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, session: DB, _: Reviewer) -> RunOut:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return _run_out(run)


@app.get("/api/findings")
def list_findings(
    session: DB,
    _: Reviewer,
    status: str | None = None,
    category: str | None = None,
    article_id: int | None = None,
    limit: int = Query(100, le=500),
) -> list[FindingSummary]:
    rank = case({"high": 0, "medium": 1, "low": 2}, value=Finding.priority, else_=3)
    q = _finding_query().order_by(rank, Finding.id.desc()).limit(limit)
    if status:
        q = q.where(Finding.status == status)
    if category:
        q = q.where(Finding.category == category)
    if article_id:
        q = q.where(Finding.article_id == article_id)
    return [FindingSummary(**_summary(f)) for f in session.scalars(q)]


@app.get("/api/findings/{finding_id}")
def get_finding(finding_id: int, session: DB, _: Reviewer) -> FindingDetail:
    f = _get_finding(session, finding_id)
    v = f.verification
    claim = v.claim
    latest = latest_source_verdicts(session, claim)
    sources = []
    for n, citation in enumerate(claim.citations, start=1):
        sv = latest.get(citation.id)
        snap = sv.snapshot if sv else None
        sources.append(
            SourceOut(
                number=n,
                citation_id=citation.id,
                citation_text=citation.text,
                citation_urls=citation.urls,
                citation_archive_url=citation.archive_url,
                citation_quote=citation.quote,
                verdict=sv.verdict if sv else None,
                explanation=sv.explanation if sv else "",
                source_url=(snap.final_url or snap.requested_url) if snap else None,
                source_via_archive=bool(snap and snap.via_archive),
                source_status=snap.status if snap else None,
                source_fetched_at=snap.fetched_at if snap else None,
            )
        )
    number_of = {s.citation_id: s.number for s in sources}
    if v.kind == "source" and v.citation_id is not None:
        quote_citation = v.citation_id  # single-source quotes predate citation_id
    else:
        quote_citation = None
    quotes = [
        {**q, "source_number": number_of.get(q.get("citation_id") or quote_citation)}
        for q in v.quotes
    ]
    return FindingDetail(
        **_summary(f),
        permalink=permalink(f.article.lang, claim.revision.rev_id),
        context_before=claim.context_before,
        assessment=v.kind,
        details=v.details or {},
        explanation=v.explanation,
        unsupported_parts=v.unsupported_parts,
        quotes=quotes,
        sources=sources,
        model=v.model,
        served_model=v.served_model,
        prompt_version=v.prompt_version,
        reviews=[
            ReviewOut(
                reviewer=r.reviewer, decision=r.decision, note=r.note, created_at=r.created_at
            )
            for r in f.reviews
        ],
    )


@app.post("/api/findings/{finding_id}/reviews", status_code=201)
def review_finding(finding_id: int, body: ReviewIn, session: DB, reviewer: Reviewer) -> dict:
    f = _get_finding(session, finding_id)
    session.add(Review(finding=f, reviewer=reviewer, decision=body.decision, note=body.note))
    f.status = FindingStatus(body.decision)
    session.flush()
    return {"id": f.id, "status": f.status}


@app.get("/api/findings/{finding_id}/talk-draft", response_class=PlainTextResponse)
def finding_talk_draft(finding_id: int, session: DB, _: Reviewer) -> str:
    f = _get_finding(session, finding_id)
    try:
        return talk_page_draft(f)
    except DraftNotAllowed as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/stats")
def stats(session: DB, _: Reviewer) -> dict:
    """Numbers to watch: how verdicts are distributed, and how often reviewers
    agree with the tool (a rough precision measure)."""
    verdicts = Counter(
        dict(
            session.execute(
                select(Verification.verdict, func.count())
                .where(Verification.kind == "source")
                .group_by(Verification.verdict)
            ).all()
        )
    )
    statuses = Counter(
        dict(session.execute(select(Finding.status, func.count()).group_by(Finding.status)).all())
    )
    decided = statuses["confirmed"] + statuses["dismissed"]
    return {
        "articles": session.scalar(select(func.count()).select_from(Article)),
        "verifications": sum(verdicts.values()),
        "verdicts": dict(verdicts),
        "findings": dict(statuses),
        "reviewer_agreement": round(statuses["confirmed"] / decided, 3) if decided else None,
        "rejected_for_unverified_quotes": session.scalar(
            select(func.count())
            .select_from(Verification)
            .where(Verification.quotes_verified.is_(False))
        ),
    }


# --- Topic collections -------------------------------------------------------------

# Rough size of article text per citation, for estimating cost before an article has
# been parsed. Refined once there's data; shown to reviewers as an estimate only.
BYTES_PER_CITATION = 1500


def run_discovery(topic: str, lang: str) -> DiscoveryResult:
    return discover(WikipediaClient(lang=lang), ClaudeRanker(), topic)


def get_discovery() -> Callable[[str, str], DiscoveryResult]:
    return run_discovery


Discovery = Annotated[Callable[[str, str], DiscoveryResult], Depends(get_discovery)]


class CollectionIn(BaseModel):
    topic: str = Field(min_length=2, max_length=300)
    lang: str = Field(default="en", pattern=r"^[a-z\-]{2,12}$")


class LastRun(BaseModel):
    id: int
    status: str
    finished_at: datetime | None
    cost_usd: float


class CollectionArticleOut(BaseModel):
    id: int
    title: str
    url: str
    relevance: int
    relevance_note: str
    description: str
    found_via: str
    size_bytes: int | None
    edit_protection: str | None
    last_edited: datetime | None
    selected: bool
    estimated_cost_usd: float
    last_run: LastRun | None
    open_findings: int
    open_uncited: int


class CollectionSummary(BaseModel):
    id: int
    topic: str
    lang: str
    created_by: str | None
    created_at: datetime
    article_count: int
    selected_count: int


class CollectionDetail(CollectionSummary):
    cost_per_citation: float
    cost_basis_runs: int
    articles: list[CollectionArticleOut]


class ScanIn(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=200)


def _collection_summary(c: Collection) -> dict:
    return {
        "id": c.id,
        "topic": c.topic,
        "lang": c.lang,
        "created_by": c.created_by,
        "created_at": c.created_at,
        "article_count": len(c.articles),
        "selected_count": sum(1 for a in c.articles if a.selected),
    }


def _collection_detail(session: Session, c: Collection) -> CollectionDetail:
    per_citation, basis = cost_per_citation(session)
    titles = [a.title for a in c.articles]
    runs: dict[str, AnalysisRun] = {}
    for r in session.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.lang == c.lang, AnalysisRun.title.in_(titles))
        .order_by(AnalysisRun.id)
    ):
        runs[r.title] = r
    open_counts = Counter()
    for title, category in session.execute(
        select(Article.title, Finding.category)
        .join(Finding, Finding.article_id == Article.id)
        .where(
            Article.lang == c.lang,
            Article.title.in_(titles),
            Finding.status.in_([FindingStatus.open, FindingStatus.needs_research]),
        )
    ):
        open_counts[(title, category)] += 1

    articles = []
    for a in c.articles:
        r = runs.get(a.title)
        est_citations = max(1, round((a.size_bytes or 0) / BYTES_PER_CITATION))
        articles.append(
            CollectionArticleOut(
                id=a.id,
                title=a.title,
                url=f"https://{c.lang}.wikipedia.org/wiki/{a.title.replace(' ', '_')}",
                relevance=a.relevance,
                relevance_note=a.relevance_note,
                description=a.description,
                found_via=a.found_via,
                size_bytes=a.size_bytes,
                edit_protection=a.edit_protection,
                last_edited=a.last_edited,
                selected=a.selected,
                estimated_cost_usd=round(est_citations * per_citation, 2),
                last_run=LastRun(
                    id=r.id,
                    status=r.status,
                    finished_at=r.finished_at,
                    cost_usd=run_cost(session, r.id),
                )
                if r
                else None,
                open_findings=open_counts[(a.title, "F2")],
                open_uncited=open_counts[(a.title, "uncited")],
            )
        )
    return CollectionDetail(
        **_collection_summary(c),
        cost_per_citation=round(per_citation, 4),
        cost_basis_runs=basis,
        articles=articles,
    )


@app.post("/api/collections", status_code=201)
def create_collection(
    body: CollectionIn, session: DB, reviewer: Reviewer, find: Discovery
) -> CollectionDetail:
    """Find the Wikipedia articles for a topic and save them as a collection.
    Takes up to a minute: it makes several Wikipedia requests and one model call."""
    try:
        result = find(body.topic.strip(), body.lang)
    except JudgeConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (httpx.HTTPError, WikipediaError) as exc:
        raise HTTPException(502, f"Wikipedia request failed: {exc}") from exc
    except JudgeError as exc:
        raise HTTPException(502, f"Ranking the articles failed: {exc}") from exc
    c = Collection(
        topic=body.topic.strip(),
        lang=body.lang,
        created_by=reviewer,
        details={
            "ranking_model": result.model,
            "ranking_version": RANKING_VERSION,
            "ranking_usage": result.usage,
            "sources": result.sources,
        },
    )
    session.add(c)
    for rank, cand in enumerate(result.candidates):
        session.add(
            CollectionArticle(
                collection=c,
                title=cand.title,
                rank=rank,
                relevance=cand.relevance,
                relevance_note=cand.note,
                description=cand.description,
                found_via=cand.found_via,
                size_bytes=cand.size,
                edit_protection=cand.edit_protection,
                last_edited=cand.last_edited,
            )
        )
    session.flush()
    session.refresh(c)
    return _collection_detail(session, c)


@app.get("/api/collections")
def list_collections(session: DB, _: Reviewer) -> list[CollectionSummary]:
    rows = session.scalars(
        select(Collection).options(selectinload(Collection.articles)).order_by(Collection.id.desc())
    )
    return [CollectionSummary(**_collection_summary(c)) for c in rows]


def _get_collection(session: Session, collection_id: int) -> Collection:
    c = session.scalar(
        select(Collection)
        .options(selectinload(Collection.articles))
        .where(Collection.id == collection_id)
    )
    if c is None:
        raise HTTPException(404, "Collection not found")
    return c


@app.get("/api/collections/{collection_id}")
def get_collection(collection_id: int, session: DB, _: Reviewer) -> CollectionDetail:
    return _collection_detail(session, _get_collection(session, collection_id))


@app.post("/api/collections/{collection_id}/scan", status_code=201)
def scan_collection(collection_id: int, body: ScanIn, session: DB, reviewer: Reviewer) -> dict:
    """Queue checks for the chosen articles. Articles already queued or running are
    skipped, so pressing the button twice doesn't pay twice."""
    c = _get_collection(session, collection_id)
    by_title = {a.title: a for a in c.articles}
    unknown = [t for t in body.titles if t not in by_title]
    if unknown:
        raise HTTPException(422, f"Not in this collection: {', '.join(unknown[:5])}")
    busy = set(
        session.scalars(
            select(AnalysisRun.title).where(
                AnalysisRun.lang == c.lang,
                AnalysisRun.title.in_(body.titles),
                AnalysisRun.status.in_([RunStatus.queued, RunStatus.running]),
            )
        )
    )
    queued, skipped = [], []
    for title in dict.fromkeys(body.titles):
        by_title[title].selected = True
        if title in busy:
            skipped.append(title)
            continue
        run = enqueue(session, title, None, c.lang, requested_by=reviewer)
        run.collection_id = c.id
        queued.append(run.id)
    session.flush()
    return {"queued": len(queued), "run_ids": queued, "already_in_progress": skipped}


# --- Static web UI ----------------------------------------------------------------

_DIST = Path(get_settings().wcm_web_dist)
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        return FileResponse(_DIST / "index.html")
