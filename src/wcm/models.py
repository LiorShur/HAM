"""Database schema.

Everything a finding depends on is stored so it can be audited later: the exact
article revision, the citation as it appeared in that revision, the source snapshot
(URL, time, content hash), the model and prompt version that judged it, and every
reviewer decision.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Verdict(StrEnum):
    supported = "supported"
    partially_supported = "partially_supported"
    not_supported = "not_supported"
    contradicted = "contradicted"
    # The source was retrieved but doesn't contain enough to judge
    # (abstract only, paywall page, wrong page of a book, etc.).
    source_insufficient = "source_insufficient"
    # Set by the pipeline, not the model: no URL, fetch failed, unsupported format.
    source_unavailable = "source_unavailable"


class FindingStatus(StrEnum):
    open = "open"
    confirmed = "confirmed"
    dismissed = "dismissed"
    needs_research = "needs_research"


class ReviewDecision(StrEnum):
    confirmed = "confirmed"
    dismissed = "dismissed"
    needs_research = "needs_research"


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("lang", "title"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lang: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    revisions: Mapped[list["Revision"]] = relationship(back_populates="article")


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("article_id", "rev_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    rev_id: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    html_sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped[Article] = relationship(back_populates="revisions")
    citations: Mapped[list["Citation"]] = relationship(back_populates="revision")
    claims: Mapped[list["Claim"]] = relationship(back_populates="revision")


claim_citations = Table(
    "claim_citations",
    Base.metadata,
    Column("claim_id", ForeignKey("claims.id"), primary_key=True),
    Column("citation_id", ForeignKey("citations.id"), primary_key=True),
)


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (UniqueConstraint("revision_id", "note_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    note_id: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    archive_url: Mapped[str | None] = mapped_column(Text)
    access_date: Mapped[str | None] = mapped_column(String(64))
    # Some citation templates carry a |quote= parameter; it's useful context.
    quote: Mapped[str | None] = mapped_column(Text)

    revision: Mapped[Revision] = relationship(back_populates="citations")
    claims: Mapped[list["Claim"]] = relationship(
        secondary=claim_citations, back_populates="citations"
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    section: Mapped[str] = mapped_column(String(512), default="")
    paragraph_index: Mapped[int] = mapped_column(Integer)
    sentence_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Uncited sentences just before this one in the same paragraph. A footnote
    # often covers them too, so the judge sees them as context.
    context_before: Mapped[str] = mapped_column(Text, default="")

    revision: Mapped[Revision] = relationship(back_populates="claims")
    citations: Mapped[list[Citation]] = relationship(
        secondary=claim_citations, back_populates="claims"
    )


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    via_archive: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    text_chars: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lang: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    requested_rev_id: Mapped[int | None] = mapped_column(Integer)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("revisions.id"))
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.queued)
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    revision: Mapped[Revision | None] = relationship()


class Verification(Base):
    """One judgement: does this citation's source support this claim?"""

    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"))
    citation_id: Mapped[int] = mapped_column(ForeignKey("citations.id"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))
    verdict: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[str] = mapped_column(Text, default="")
    unsupported_parts: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"kind": "supporting"|"contradicting", "text": ..., "translation": ...,
    #   "verified": bool, "excerpt": ...}]
    quotes: Mapped[list[dict]] = mapped_column(JSON, default=list)
    # False if the model quoted text that isn't actually in the source.
    quotes_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    model_confidence: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(String(64))
    served_model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    claim: Mapped[Claim] = relationship()
    citation: Mapped[Citation] = relationship()
    snapshot: Mapped[SourceSnapshot | None] = relationship()
    run: Mapped[AnalysisRun] = relationship()


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    verification_id: Mapped[int] = mapped_column(ForeignKey("verifications.id"), unique=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    # F2 = citation mismatch. Later phases add F1, F3, F4, F5.
    category: Mapped[str] = mapped_column(String(8), default="F2")
    priority: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default=FindingStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    verification: Mapped[Verification] = relationship()
    article: Mapped[Article] = relationship()
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="finding", order_by="Review.created_at"
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"))
    reviewer: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    finding: Mapped[Finding] = relationship(back_populates="reviews")
