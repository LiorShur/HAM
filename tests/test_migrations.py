"""`wcm init-db` must create new databases and upgrade ones made by older versions."""

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from alembic import command
from wcm.config import get_settings
from wcm.db import INITIAL_REVISION, migrate
from wcm.models import Base


@pytest.fixture
def url(tmp_path):
    pg = os.environ.get("TEST_DATABASE_URL")
    if pg:
        eng = create_engine(pg)
        with eng.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        eng.dispose()
        return pg
    return f"sqlite:///{tmp_path / 'm.db'}"


def _schema_diff(url):
    eng = create_engine(url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    eng.dispose()
    return diff


def test_new_database_matches_the_models(url):
    assert migrate(url).startswith("New database")
    assert _schema_diff(url) == []
    assert migrate(url).startswith("Existing database;")  # idempotent


def test_database_from_old_init_db_is_upgraded_and_findings_linked_to_claims(url):
    # Recreate what the old `wcm init-db` produced: the first schema, no history.
    cfg = Config()
    cfg.set_main_option("script_location", get_settings().wcm_alembic_dir)
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, INITIAL_REVISION)
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP TABLE alembic_version"))
        c.execute(
            text(
                "INSERT INTO articles (id, lang, title, created_at) "
                "VALUES (1, 'en', 'A', CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO revisions (id, article_id, rev_id, html_sha256, fetched_at) "
                "VALUES (1, 1, 5, 'x', CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO claims (id, revision_id, section, paragraph_index, "
                "sentence_index, text, context_before) "
                "VALUES (7, 1, '', 0, 0, 'c', '')"
            )
        )
        c.execute(
            text(
                "INSERT INTO citations (id, revision_id, note_id, text, urls) "
                "VALUES (3, 1, 'n', 't', '[]')"
            )
        )
        c.execute(
            text(
                "INSERT INTO analysis_runs (id, lang, title, status, stats, created_at) "
                "VALUES (1, 'en', 'A', 'done', '{}', CURRENT_TIMESTAMP)"
            )
        )
        c.execute(
            text(
                "INSERT INTO verifications (id, run_id, claim_id, citation_id, verdict, "
                "explanation, unsupported_parts, quotes, quotes_verified, usage, "
                "created_at) VALUES (1, 1, 7, 3, 'not_supported', '', '[]', '[]', "
                ":t, '{}', CURRENT_TIMESTAMP)"
            ),
            {"t": True},
        )
        c.execute(
            text(
                "INSERT INTO findings (id, verification_id, article_id, category, "
                "priority, status, created_at, updated_at) VALUES "
                "(1, 1, 1, 'F2', 'high', 'open', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()

    assert migrate(url).startswith("Existing database recorded")
    assert _schema_diff(url) == []
    eng = create_engine(url)
    with eng.connect() as c:
        row = c.execute(text("SELECT claim_id, needs_offline_check FROM findings")).one()
        kind = c.execute(text("SELECT kind FROM verifications")).scalar_one()
    eng.dispose()
    assert row[0] == 7 and not row[1]
    assert kind == "source"
    assert "verifications" in inspect(create_engine(url)).get_table_names()
