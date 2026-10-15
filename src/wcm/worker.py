"""A small database-backed job queue for analysis runs.

Runs are rows in `analysis_runs`; workers claim the oldest queued row with
SELECT ... FOR UPDATE SKIP LOCKED, so several workers can run side by side on
Postgres without picking up the same job.
"""

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from wcm.db import session_scope
from wcm.judge import ClaudeJudge, Judge
from wcm.models import AnalysisRun, RunStatus
from wcm.pipeline import Analyzer
from wcm.sources import SourceFetcher
from wcm.wikipedia import WikipediaClient

log = logging.getLogger(__name__)


def enqueue(
    session: Session,
    title: str | None,
    rev_id: int | None = None,
    lang: str = "en",
    requested_by: str | None = None,
) -> AnalysisRun:
    if not title and rev_id is None:
        raise ValueError("title or rev_id required")
    run = AnalysisRun(
        lang=lang,
        title=title or "",
        requested_rev_id=rev_id,
        requested_by=requested_by,
        status=RunStatus.queued,
    )
    session.add(run)
    session.flush()
    return run


def claim_next(session: Session) -> AnalysisRun | None:
    stmt = (
        select(AnalysisRun)
        .where(AnalysisRun.status == RunStatus.queued)
        .order_by(AnalysisRun.created_at, AnalysisRun.id)
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    run = session.scalar(stmt)
    if run:
        run.status = RunStatus.running
        session.flush()
    return run


def process(
    session: Session,
    run: AnalysisRun,
    judge: Judge | None = None,
    wiki: WikipediaClient | None = None,
    fetcher: SourceFetcher | None = None,
    recheck: bool = False,
) -> None:
    Analyzer(
        session,
        wiki or WikipediaClient(lang=run.lang),
        fetcher or SourceFetcher(),
        judge or ClaudeJudge(),
        recheck=recheck,
    ).run(run)


def work_once(judge: Judge | None = None) -> bool:
    # Claim in its own transaction so the "running" status is visible right away.
    with session_scope() as session:
        run = claim_next(session)
        if run is None:
            return False
        run_id = run.id
    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        log.info("Processing run %s (%s)", run.id, run.title or run.requested_rev_id)
        process(session, run, judge=judge)
        log.info("Run %s finished: %s %s", run.id, run.status, run.stats)
    return True


def work_forever(poll_seconds: float = 5.0, judge: Judge | None = None) -> None:
    judge = judge or ClaudeJudge()
    log.info("Worker started; polling every %ss for queued runs", poll_seconds)
    while True:
        try:
            busy = work_once(judge)
        except Exception:
            # e.g. the database was briefly unavailable; keep the worker alive.
            log.exception("Worker error; retrying in %ss", poll_seconds)
            busy = False
        if not busy:
            time.sleep(poll_seconds)
