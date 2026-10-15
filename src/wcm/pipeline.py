"""One analysis run: fetch a revision, extract claims, check each citation's source."""

import hashlib
import logging
from collections import Counter

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wcm.config import get_settings
from wcm.judge import (
    PROMPT_VERSION,
    ClaimInput,
    ClaimJudgement,
    Judge,
    JudgeConfigError,
    JudgeError,
)
from wcm.models import (
    AnalysisRun,
    Article,
    Citation,
    Claim,
    Finding,
    Revision,
    RunStatus,
    SourceSnapshot,
    Verdict,
    Verification,
    utcnow,
)
from wcm.parsing import parse_article
from wcm.sources import (
    FetchResult,
    SourceFetcher,
    excerpt,
    find_quote,
    parse_access_date,
    select_passages,
)
from wcm.wikipedia import WikipediaClient

log = logging.getLogger(__name__)

# Errors that will repeat for every source, so the run stops instead of going on.
FATAL_ERRORS = (
    JudgeConfigError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)

NEGATIVE = {
    Verdict.not_supported,
    Verdict.contradicted,
    Verdict.partially_supported,
    Verdict.source_insufficient,
}
FINDING_VERDICTS = {Verdict.not_supported, Verdict.contradicted, Verdict.partially_supported}


def get_or_create_revision(
    session: Session, wiki: WikipediaClient, title: str | None, rev_id: int | None
) -> Revision:
    info = wiki.revision_info(title=title, rev_id=rev_id)
    article = session.scalar(
        select(Article).where(Article.lang == wiki.lang, Article.title == info.title)
    )
    if article is None:
        article = Article(lang=wiki.lang, title=info.title)
        session.add(article)
        session.flush()

    revision = session.scalar(
        select(Revision).where(Revision.article_id == article.id, Revision.rev_id == info.rev_id)
    )
    if revision is not None:
        return revision

    html = wiki.revision_html(info.rev_id)
    parsed = parse_article(html)
    revision = Revision(
        article=article,
        rev_id=info.rev_id,
        timestamp=info.timestamp,
        html_sha256=hashlib.sha256(html.encode()).hexdigest(),
    )
    session.add(revision)
    session.flush()

    citations: dict[str, Citation] = {}
    for note_id, pc in parsed.citations.items():
        c = Citation(
            revision=revision,
            note_id=note_id,
            text=pc.text,
            urls=pc.urls,
            archive_url=pc.archive_url,
            access_date=pc.access_date,
            quote=pc.quote,
        )
        session.add(c)
        citations[note_id] = c
    for pc in parsed.claims:
        claim = Claim(
            revision=revision,
            section=pc.section,
            paragraph_index=pc.paragraph_index,
            sentence_index=pc.sentence_index,
            text=pc.text,
            context_before=pc.context_before,
        )
        claim.citations = [citations[n] for n in pc.note_ids if n in citations]
        session.add(claim)
    session.flush()
    return revision


def _priority(j: ClaimJudgement) -> str:
    if j.verdict == Verdict.contradicted or (
        j.verdict == Verdict.not_supported and j.confidence >= 0.75
    ):
        return "high"
    if j.verdict == Verdict.not_supported or j.confidence >= 0.75:
        return "medium"
    return "low"


def check_quotes(j: ClaimJudgement, source_text: str, context: int) -> tuple[list[dict], bool]:
    quotes: list[dict] = []
    all_ok = True
    for kind, items in (
        ("supporting", j.supporting_quotes),
        ("contradicting", j.contradicting_quotes),
    ):
        for q in items:
            span = find_quote(q.text, source_text)
            quotes.append(
                {
                    "kind": kind,
                    "text": q.text,
                    "translation": q.translation or None,
                    "verified": span is not None,
                    "excerpt": excerpt(source_text, *span, context) if span else None,
                }
            )
            all_ok &= span is not None
    if j.verdict == Verdict.contradicted and not any(
        q["verified"] for q in quotes if q["kind"] == "contradicting"
    ):
        all_ok = False
    if j.verdict in (Verdict.supported, Verdict.partially_supported) and not any(
        q["verified"] for q in quotes if q["kind"] == "supporting"
    ):
        all_ok = False
    return quotes, all_ok


class Analyzer:
    def __init__(
        self,
        session: Session,
        wiki: WikipediaClient,
        fetcher: SourceFetcher,
        judge: Judge,
        recheck: bool = False,
    ):
        self.session = session
        # False: skip citations whose claims were all judged before with the current
        # prompt version (saves API cost when re-running the same revision).
        self.recheck = recheck
        self.wiki = wiki
        self.fetcher = fetcher
        self.judge = judge
        self.settings = get_settings()
        self._snapshots: dict[tuple[str, bool], tuple[FetchResult, SourceSnapshot]] = {}

    # -- fetching ------------------------------------------------------------------

    def _fetch(self, url: str, via_archive: bool) -> tuple[FetchResult, SourceSnapshot]:
        key = (url, via_archive)
        if key not in self._snapshots:
            result = self.fetcher.fetch(url, via_archive=via_archive)
            snap = SourceSnapshot(
                requested_url=url,
                final_url=result.final_url,
                via_archive=via_archive,
                status=result.status,
                http_status=result.http_status,
                content_type=result.content_type,
                title=result.title,
                sha256=result.sha256,
                text_chars=len(result.text) if result.text else None,
                error=result.error,
            )
            self.session.add(snap)
            self.session.flush()
            self._snapshots[key] = (result, snap)
        return self._snapshots[key]

    def _live(self, citation: Citation) -> tuple[FetchResult, SourceSnapshot] | None:
        last = None
        for url in citation.urls[:2]:
            last = self._fetch(url, via_archive=False)
            if last[0].ok:
                return last
        return last

    def _archive(self, citation: Citation) -> tuple[FetchResult, SourceSnapshot] | None:
        url = citation.archive_url
        if not url and citation.urls:
            url = self.fetcher.find_archive(
                citation.urls[0], parse_access_date(citation.access_date)
            )
        return self._fetch(url, via_archive=True) if url else None

    # -- judging -------------------------------------------------------------------

    def _judge(self, citation: Citation, fetched: FetchResult, claims: list[Claim]):
        text = select_passages(
            fetched.text, [c.text for c in claims], self.settings.wcm_max_source_chars
        )
        return self.judge.judge(
            citation_text=citation.text,
            source_title=fetched.title,
            source_url=fetched.final_url or fetched.requested_url,
            source_text=text,
            claims=[ClaimInput(c.id, c.text, c.context_before) for c in claims],
        ), text

    def _record_unavailable(self, run, claim, citation, snap, reason: str, stats: Counter):
        self.session.add(
            Verification(
                run=run,
                claim=claim,
                citation=citation,
                snapshot=snap,
                verdict=Verdict.source_unavailable,
                explanation=reason,
            )
        )
        stats[f"verdict:{Verdict.source_unavailable}"] += 1

    def check_citation(self, run: AnalysisRun, citation: Citation, stats: Counter) -> None:
        claims = list(citation.claims)
        if not claims:
            return
        stats["citations_checked"] += 1

        if not citation.urls and not citation.archive_url:
            for claim in claims:
                self._record_unavailable(
                    run, claim, citation, None, "Citation has no URL (e.g. a print book).", stats
                )
            return

        fetched = self._live(citation)
        if fetched is None or not fetched[0].ok:
            fetched = self._archive(citation) or fetched
        if fetched is None or not fetched[0].ok:
            snap = fetched[1] if fetched else None
            status = fetched[0].status if fetched else "no_url"
            for claim in claims:
                self._record_unavailable(
                    run, claim, citation, snap, f"Could not retrieve source ({status}).", stats
                )
            return

        result, snap = fetched
        try:
            judged, sent_text = self._judge(citation, result, claims)
        except JudgeError as exc:
            stats["judge_errors"] += 1
            run.stats = {**(run.stats or {}), "last_error": str(exc)}
            log.warning("Judge failed for citation %s: %s", citation.note_id, exc)
            return

        # A negative verdict on a live page may just mean the page changed since it
        # was cited. Re-check those claims against an archived copy before flagging.
        if not result.via_archive:
            negative = [c for c in claims if judged.judgements[c.id].verdict in NEGATIVE]
            if negative:
                archived = self._archive(citation)
                if archived and archived[0].ok:
                    try:
                        rejudged, archive_text = self._judge(citation, archived[0], negative)
                    except JudgeError as exc:
                        stats["judge_errors"] += 1
                        log.warning("Archive re-check failed: %s", exc)
                    else:
                        stats["archive_rechecks"] += 1
                        for c in negative:
                            self._store(
                                run,
                                c,
                                citation,
                                archived[1],
                                rejudged,
                                rejudged.judgements[c.id],
                                archive_text,
                                stats,
                            )
                        claims = [c for c in claims if c not in negative]

        for c in claims:
            self._store(run, c, citation, snap, judged, judged.judgements[c.id], sent_text, stats)

    def _store(
        self,
        run,
        claim,
        citation,
        snap,
        judged,
        j: ClaimJudgement,
        source_text: str,
        stats: Counter,
    ) -> None:
        quotes, quotes_ok = check_quotes(j, source_text, self.settings.wcm_excerpt_context_chars)
        v = Verification(
            run=run,
            claim=claim,
            citation=citation,
            snapshot=snap,
            verdict=j.verdict,
            explanation=j.explanation,
            unsupported_parts=j.unsupported_parts,
            quotes=quotes,
            quotes_verified=quotes_ok,
            model_confidence=j.confidence,
            model=judged.model,
            served_model=judged.served_model,
            prompt_version=PROMPT_VERSION,
            request_id=judged.request_id,
            usage=judged.usage,
        )
        self.session.add(v)
        self.session.flush()
        stats[f"verdict:{j.verdict}"] += 1

        if not quotes_ok:
            # The model cited text that isn't in the source. Don't trust the verdict.
            stats["rejected_unverified_quotes"] += 1
            return
        if j.verdict in FINDING_VERDICTS and not self._has_existing_finding(claim, citation):
            self.session.add(
                Finding(
                    verification=v,
                    article_id=run.revision.article_id,
                    category="F2",
                    priority=_priority(j),
                )
            )
            stats["findings"] += 1

    def _has_existing_finding(self, claim: Claim, citation: Citation) -> bool:
        return (
            self.session.scalar(
                select(Finding.id)
                .join(Verification)
                .where(
                    Verification.claim_id == claim.id,
                    Verification.citation_id == citation.id,
                )
                .limit(1)
            )
            is not None
        )

    def _already_checked(self, citation: Citation) -> bool:
        claim_ids = {c.id for c in citation.claims}
        if not claim_ids:
            return False
        judged = self.session.scalar(
            select(func.count(func.distinct(Verification.claim_id))).where(
                Verification.citation_id == citation.id,
                Verification.prompt_version == PROMPT_VERSION,
            )
        )
        return judged >= len(claim_ids)

    def run(self, run: AnalysisRun) -> None:
        run.status = RunStatus.running
        run.started_at = utcnow()
        self.session.commit()
        stats: Counter = Counter()
        try:
            revision = get_or_create_revision(
                self.session, self.wiki, run.title, run.requested_rev_id
            )
            run.revision = revision
            run.title = revision.article.title
            stats["claims"] = len(revision.claims)
            stats["citations"] = len(revision.citations)
            self._save_progress(run, stats)

            # Each citation is its own unit of work, committed when done. A failure
            # (a broken PDF, an unexpected response) rolls back that citation only.
            for citation_id in [c.id for c in revision.citations]:
                citation = self.session.get(Citation, citation_id)
                if not self.recheck and self._already_checked(citation):
                    stats["skipped_already_checked"] += 1
                    continue
                before = stats.copy()
                try:
                    self.check_citation(run, citation, stats)
                    self._save_progress(run, stats)
                except FATAL_ERRORS:
                    raise
                except Exception as exc:
                    log.exception("Citation %s failed; continuing", citation_id)
                    self.session.rollback()
                    self._snapshots.clear()
                    stats.clear()
                    stats.update(before)
                    stats["citation_errors"] += 1
                    run.error = f"Last citation error: {type(exc).__name__}: {exc}"
                    self._save_progress(run, stats)
            run.status = RunStatus.done
        except Exception as exc:
            log.exception("Run %s failed", run.id)
            self.session.rollback()
            run.status = RunStatus.failed
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = utcnow()
            self._save_progress(run, stats)

    def _save_progress(self, run: AnalysisRun, stats: Counter) -> None:
        run.stats = {k: v for k, v in (run.stats or {}).items() if k == "last_error"} | dict(stats)
        self.session.commit()
