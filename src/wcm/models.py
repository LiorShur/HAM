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
    false,
    true,
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
    # A sentence with no citation (Verification kind="triage").
    uncited = "uncited"


class FindingStatus(StrEnum):
    open = "open"
    confirmed = "confirmed"
    dismissed = "dismissed"
    needs_research = "needs_research"
    # Replaced by a newer assessment of the same claim (e.g. after a prompt change)
    # before anyone confirmed or dismissed it.
    superseded = "superseded"


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
    # Which parsing rules produced its claims (see pipeline.PARSER_VERSION).
    # Version 1 stored cited sentences only.
    parser_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

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
    # For cited claims: uncited sentences just before this one in the same paragraph.
    # A footnote often covers them too, so the judge sees them as context.
    # For uncited claims: the sentences just before it, to resolve references.
    context_before: Mapped[str] = mapped_column(Text, default="")
    # False: no footnote covers this sentence.
    cited: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Wikipedia already has a {{citation needed}} tag on it.
    citation_needed_tag: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )

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
    # Set when the run was queued from a topic collection.
    collection_id: Mapped[int | None] = mapped_column(ForeignKey("collections.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    revision: Mapped[Revision | None] = relationship()


class Verification(Base):
    """One judgement. kind="source": does this one citation's source support the
    claim? kind="claim": do all of the claim's sources, taken together, support it?
    (citation_id and snapshot_id are empty for kind="claim"; each quote records the
    citation it came from.) kind="triage": how much an uncited claim needs a source."""

    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"))
    kind: Mapped[str] = mapped_column(String(16), default="source", server_default="source")
    citation_id: Mapped[int | None] = mapped_column(ForeignKey("citations.id"))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))
    verdict: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[str] = mapped_column(Text, default="")
    # none | minor | major; for kind="triage": contentious | specific | general
    severity: Mapped[str | None] = mapped_column(String(16))
    # kind="triage": {"living_person", "in_lead", "citation_needed_tag": bool,
    #   "covered_by": claim id of a cited sentence stating the same fact, or None,
    #   "covered_by_text": that sentence}
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    unsupported_parts: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"kind": "supporting"|"contradicting", "citation_id": int, "text": ...,
    #   "translation": ..., "verified": bool, "excerpt": ...}]
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
    citation: Mapped[Citation | None] = relationship()
    snapshot: Mapped[SourceSnapshot | None] = relationship()
    run: Mapped[AnalysisRun] = relationship()


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    verification_id: Mapped[int] = mapped_column(ForeignKey("verifications.id"), unique=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    # At most one live finding per claim.
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id"))
    # The readable sources don't support the claim, but it also cites sources that
    # couldn't be checked online (books, dead links), which may cover the rest.
    needs_offline_check: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # F2 = citation mismatch; "uncited" = no citation for a statement that needs one.
    # Later phases add F1, F3, F4, F5.
    category: Mapped[str] = mapped_column(String(8), default="F2")
    priority: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default=FindingStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    verification: Mapped[Verification] = relationship()
    article: Mapped[Article] = relationship()
    claim: Mapped[Claim | None] = relationship()
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


class Collection(Base):
    """A saved topic search: the Wikipedia articles found for a subject, which of
    them a reviewer chose to scan, and the runs queued for them."""

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(512))
    lang: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Which model ranked the candidates, and the discovery settings used.
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")

    articles: Mapped[list["CollectionArticle"]] = relationship(
        back_populates="collection", order_by="CollectionArticle.rank"
    )


class CollectionArticle(Base):
    __tablename__ = "collection_articles"
    __table_args__ = (UniqueConstraint("collection_id", "title"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    title: Mapped[str] = mapped_column(String(512))
    # Order shown to the reviewer: most relevant first.
    rank: Mapped[int] = mapped_column(Integer)
    # 3 core to the topic, 2 closely related, 1 tangential, 0 unrelated.
    relevance: Mapped[int] = mapped_column(Integer)
    relevance_note: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # How it was found: search, category:<name>, linked from <title>.
    found_via: Mapped[str] = mapped_column(String(512), default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    # e.g. "extendedconfirmed": only editors with 500 edits and 30 days can edit.
    edit_protection: Mapped[str | None] = mapped_column(String(64))
    last_edited: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    collection: Mapped[Collection] = relationship(back_populates="articles")
