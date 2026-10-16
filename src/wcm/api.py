from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from wcm.config import get_settings
from wcm.db import session_factory
from wcm.models import (
    AnalysisRun,
    Article,
    Finding,
    FindingStatus,
    Review,
    Verification,
)
from wcm.pipeline import latest_source_verdicts
from wcm.reports import DraftNotAllowed, talk_page_draft
from wcm.wikipedia import permalink
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


class FindingSummary(BaseModel):
    id: int
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
    # claim's readable sources were judged together.
    assessment: str
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
    return RunOut(
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
    article_id: int | None = None,
    limit: int = Query(100, le=500),
) -> list[FindingSummary]:
    q = _finding_query().order_by(Finding.id.desc()).limit(limit)
    if status:
        q = q.where(Finding.status == status)
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


# --- Static web UI ----------------------------------------------------------------

_DIST = Path(get_settings().wcm_web_dist)
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        return FileResponse(_DIST / "index.html")
