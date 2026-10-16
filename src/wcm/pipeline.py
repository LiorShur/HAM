"""One analysis run: fetch a revision, extract claims, check them against their sources.

Stage 1, per citation: fetch the source once and judge every claim that cites it
(Verification kind="source"). These are kept for the audit trail.

Stage 2, per claim: combine the results for all of the claim's citations. If any one
source supports it, it's supported. If several sources each support only part, judge
them together (kind="claim"), since Wikipedia often cites one source per part of a
sentence. A claim gets at most one live finding.
"""

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
    SourceInput,
)
from wcm.models import (
    AnalysisRun,
    Article,
    Citation,
    Claim,
    Finding,
    FindingStatus,
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


def _priority(v: Verification, needs_offline_check: bool) -> str:
    if v.verdict == Verdict.contradicted and v.severity != "minor":
        return "high"
    if needs_offline_check or v.severity == "minor":
        return "low"
    confident = (v.model_confidence or 0) >= 0.75
    if v.verdict == Verdict.not_supported and confident:
        return "high"
    if v.verdict == Verdict.not_supported or confident:
        return "medium"
    return "low"


def check_quotes(
    j: ClaimJudgement, sources: dict[int, tuple[int | None, str]], context: int
) -> tuple[list[dict], bool]:
    """Check every quote against the text of the source it names.

    `sources` maps the source number shown to the model to (citation_id, text).
    """
    quotes: list[dict] = []
    all_ok = True
    for kind, items in (
        ("supporting", j.supporting_quotes),
        ("contradicting", j.contradicting_quotes),
    ):
        for q in items:
            src = sources.get(q.source_id)
            span = find_quote(q.text, src[1]) if src else None
            quotes.append(
                {
                    "kind": kind,
                    "citation_id": src[0] if src else None,
                    "text": q.text,
                    "translation": q.translation or None,
                    "verified": span is not None,
                    "excerpt": excerpt(src[1], *span, context) if span else None,
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


def latest_source_verdicts(session: Session, claim: Claim) -> dict[int, Verification]:
    """The most recent per-source verdict for each of the claim's citations."""
    latest: dict[int, Verification] = {}
    rows = session.scalars(
        select(Verification)
        .where(Verification.claim_id == claim.id, Verification.kind == "source")
        .order_by(Verification.id)
    )
    for v in rows:
        latest[v.citation_id] = v
    return latest


UNREADABLE = {Verdict.source_unavailable, Verdict.source_insufficient}
UNDECIDED = {FindingStatus.open, FindingStatus.needs_research}


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

    # -- stage 1: one source, every claim that cites it --------------------------------

    def _judge(self, citation: Citation, fetched: FetchResult, claims: list[Claim]):
        text = select_passages(
            fetched.text, [c.text for c in claims], self.settings.wcm_max_source_chars
        )
        source = SourceInput(
            source_id=1,
            citation_text=citation.text,
            url=fetched.final_url or fetched.requested_url,
            text=text,
            title=fetched.title,
        )
        result = self.judge.judge(
            sources=[source],
            claims=[ClaimInput(c.id, c.text, c.context_before) for c in claims],
        )
        return result, text

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
            run.error = f"Last judge error: {exc}"
            log.warning("Judge failed for citation %s: %s", citation.note_id, exc)
            return

        # A negative verdict on a live page may just mean the page changed since it
        # was cited. Re-check those claims against an archived copy.
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
                            self._store_source_verdict(
                                run, c, citation, archived[1], rejudged, archive_text, stats
                            )
                        claims = [c for c in claims if c not in negative]

        for c in claims:
            self._store_source_verdict(run, c, citation, snap, judged, sent_text, stats)

    def _store_source_verdict(self, run, claim, citation, snap, judged, text, stats) -> None:
        j = judged.judgements[claim.id]
        quotes, quotes_ok = check_quotes(
            j, {1: (citation.id, text)}, self.settings.wcm_excerpt_context_chars
        )
        self._add_verification(run, claim, "source", citation, snap, judged, j, quotes, quotes_ok)
        stats[f"verdict:{j.verdict}"] += 1
        if not quotes_ok:
            # The model quoted text that isn't in the source. Don't trust the verdict.
            stats["rejected_unverified_quotes"] += 1

    def _add_verification(
        self, run, claim, kind, citation, snap, judged, j, quotes, quotes_ok
    ) -> Verification:
        v = Verification(
            run=run,
            claim=claim,
            kind=kind,
            citation=citation,
            snapshot=snap,
            verdict=j.verdict,
            severity=j.severity,
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
        return v

    # -- stage 2: one claim, all of its sources ---------------------------------------

    def assess_claim(self, run: AnalysisRun, claim: Claim, stats: Counter) -> None:
        latest = latest_source_verdicts(self.session, claim)
        judged: list[Verification] = []
        unreadable: list[Citation] = []
        for citation in claim.citations:
            v = latest.get(citation.id)
            if v is None or v.verdict in UNREADABLE:
                unreadable.append(citation)
            elif v.prompt_version != PROMPT_VERSION:
                # Judged with an older prompt and not re-checked this run (error).
                continue
            elif v.quotes_verified:
                judged.append(v)
        if not judged:
            return
        stats["claims_assessed"] += 1

        if any(v.verdict == Verdict.supported for v in judged):
            stats["claims_supported"] += 1
            self._settle(run, claim, None, False, stats)
            return

        if len(judged) >= 2:
            final = self._judge_together(run, claim, judged, stats)
            if final is None:
                return
        else:
            final = judged[0]

        if final.verdict not in FINDING_VERDICTS:
            # Supported once the sources are read together, or can't be decided.
            if final.verdict == Verdict.supported:
                stats["claims_supported"] += 1
            self._settle(run, claim, None, False, stats)
            return
        needs_offline = bool(unreadable) and final.verdict != Verdict.contradicted
        self._settle(run, claim, final, needs_offline, stats)

    def _judge_together(
        self, run: AnalysisRun, claim: Claim, judged: list[Verification], stats: Counter
    ) -> Verification | None:
        cap = self.settings.wcm_max_source_chars // len(judged)
        sources: list[SourceInput] = []
        texts: dict[int, tuple[int | None, str]] = {}
        for v in judged:
            fetched, _ = self._fetch(v.snapshot.requested_url, v.snapshot.via_archive)
            if not fetched.ok:
                continue
            n = len(sources) + 1
            text = select_passages(fetched.text, [claim.text], cap)
            sources.append(
                SourceInput(
                    source_id=n,
                    citation_text=v.citation.text,
                    url=fetched.final_url or fetched.requested_url,
                    text=text,
                    title=fetched.title,
                )
            )
            texts[n] = (v.citation_id, text)
        if len(sources) < 2:
            return judged[0]
        try:
            result = self.judge.judge(
                sources=sources, claims=[ClaimInput(claim.id, claim.text, claim.context_before)]
            )
        except JudgeError as exc:
            stats["judge_errors"] += 1
            run.error = f"Last judge error: {exc}"
            log.warning("Combined check failed for claim %s: %s", claim.id, exc)
            return None
        j = result.judgements[claim.id]
        quotes, quotes_ok = check_quotes(j, texts, self.settings.wcm_excerpt_context_chars)
        stats["combined_checks"] += 1
        stats[f"combined:{j.verdict}"] += 1
        v = self._add_verification(run, claim, "claim", None, None, result, j, quotes, quotes_ok)
        if not quotes_ok:
            stats["rejected_unverified_quotes"] += 1
            return None
        return v

    def _settle(
        self,
        run: AnalysisRun,
        claim: Claim,
        final: Verification | None,
        needs_offline: bool,
        stats: Counter,
    ) -> None:
        """Record the claim's outcome: at most one live finding per claim.

        Findings a person has confirmed or dismissed are left alone. Undecided
        findings from an older prompt version are marked superseded by this
        assessment."""
        existing = self.session.scalars(
            select(Finding).where(Finding.claim_id == claim.id).order_by(Finding.id)
        ).all()
        if any(f.status in (FindingStatus.confirmed, FindingStatus.dismissed) for f in existing):
            return
        live = []
        for f in existing:
            if f.status not in UNDECIDED:
                continue
            if f.verification.prompt_version != PROMPT_VERSION:
                f.status = FindingStatus.superseded
                stats["superseded_findings"] += 1
            else:
                live.append(f)
        if final is None or live:
            return
        self.session.add(
            Finding(
                verification=final,
                article_id=run.revision.article_id,
                claim_id=claim.id,
                category="F2",
                priority=_priority(final, needs_offline),
                needs_offline_check=needs_offline,
            )
        )
        stats["findings"] += 1
        if needs_offline:
            stats["needs_offline_check"] += 1

    # -- the run ---------------------------------------------------------------------

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

    def _unit(self, run: AnalysisRun, stats: Counter, label: str, fn) -> None:
        """Run one unit of work and commit it. An unexpected error rolls back this
        unit only and the run continues; setup errors stop the run."""
        before = stats.copy()
        try:
            fn()
            self._save_progress(run, stats)
        except FATAL_ERRORS:
            raise
        except Exception as exc:
            log.exception("%s failed; continuing", label)
            self.session.rollback()
            self._snapshots.clear()
            stats.clear()
            stats.update(before)
            stats["unit_errors"] += 1
            run.error = f"{label}: {type(exc).__name__}: {exc}"
            self._save_progress(run, stats)

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

            for citation_id in [c.id for c in revision.citations]:
                citation = self.session.get(Citation, citation_id)
                if not self.recheck and self._already_checked(citation):
                    stats["skipped_already_checked"] += 1
                    continue
                self._unit(
                    run,
                    stats,
                    f"Citation {citation.note_id}",
                    lambda c=citation: self.check_citation(run, c, stats),
                )

            for claim_id in [c.id for c in revision.claims]:
                self._unit(
                    run,
                    stats,
                    f"Claim {claim_id}",
                    lambda cid=claim_id: self.assess_claim(
                        run, self.session.get(Claim, cid), stats
                    ),
                )
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
        run.stats = dict(stats)
        self.session.commit()
