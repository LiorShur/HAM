import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from wcm.config import get_settings
from wcm.judge import ClaimJudgement, JudgeResult, Quote
from wcm.models import Base
from wcm.sources import FetchResult
from wcm.wikipedia import RevisionInfo

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("WCM_REVIEWER_TOKENS", "alice:tok-a,bob:tok-b")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine(tmp_path):
    # Set TEST_DATABASE_URL to run against Postgres (CI does). Tables are reset.
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        eng = create_engine(url)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine(
            f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
        )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


class FakeWiki:
    lang = "en"

    def revision_info(self, title=None, rev_id=None):
        return RevisionInfo(title="Harbor Point Lighthouse", rev_id=rev_id or 1000, timestamp=None)

    def revision_html(self, rev_id):
        return (FIXTURES / "parsoid_sample.html").read_text()


class FakeFetcher:
    """Serves canned text per URL. Archive URLs are keyed with an 'archive:' prefix."""

    def __init__(self, pages: dict[str, str], archive_lookup: dict[str, str] | None = None):
        self.pages = pages
        self.archive_lookup = archive_lookup or {}
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, url, via_archive=False):
        self.calls.append((url, via_archive))
        key = ("archive:" if via_archive else "") + url
        if key not in self.pages:
            return FetchResult(url, "http_error", http_status=404, via_archive=via_archive)
        return FetchResult(
            url,
            "ok",
            text=self.pages[key],
            final_url=url,
            via_archive=via_archive,
            http_status=200,
            content_type="text/html",
        )

    def find_archive(self, url, near):
        return self.archive_lookup.get(url)


def judgement(claim_id, verdict, supporting=(), contradicting=(), confidence=0.9, parts=()):
    return ClaimJudgement(
        claim_id=claim_id,
        verdict=verdict,
        supporting_quotes=[Quote(text=t, translation="") for t in supporting],
        contradicting_quotes=[Quote(text=t, translation="") for t in contradicting],
        unsupported_parts=list(parts),
        explanation="test",
        confidence=confidence,
    )


class FakeJudge:
    """Decides by looking up (claim text prefix, source text) in a rule function."""

    def __init__(self, rule):
        self.rule = rule
        self.calls = []

    def judge(self, *, citation_text, source_title, source_url, source_text, claims):
        self.calls.append((source_url, [c.text for c in claims]))
        return JudgeResult(
            judgements={c.claim_id: self.rule(c, source_text) for c in claims},
            model="fake-model",
            served_model="fake-model",
            request_id="req_test",
        )
