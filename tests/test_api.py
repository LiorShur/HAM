import pytest
from conftest import FakeJudge
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from test_pipeline import rule, run_pipeline

from wcm import api

A = {"Authorization": "Bearer tok-a"}


@pytest.fixture
def client(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        s = factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    api.app.dependency_overrides[api.get_session] = override
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_auth_required(client):
    assert client.get("/api/findings").status_code == 401
    assert client.get("/api/findings", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=A).json() == {"reviewer": "alice"}


def test_create_and_list_runs(client):
    r = client.post("/api/runs", json={"title": "Harbor Point Lighthouse"}, headers=A)
    assert r.status_code == 201 and r.json()["status"] == "queued"
    assert r.json()["requested_by"] == "alice"
    assert client.post("/api/runs", json={}, headers=A).status_code == 422
    assert len(client.get("/api/runs", headers=A).json()) == 1


def test_review_flow_and_talk_draft(client, session):
    run_pipeline(session, judge=FakeJudge(rule))
    findings = client.get("/api/findings?status=open", headers=A).json()
    assert len(findings) == 1
    fid = findings[0]["id"]

    detail = client.get(f"/api/findings/{fid}", headers=A).json()
    assert detail["verdict"] == "contradicted"
    assert detail["permalink"] == "https://en.wikipedia.org/w/index.php?oldid=1000"
    assert detail["source_via_archive"] is True

    # No draft until a human confirms.
    assert client.get(f"/api/findings/{fid}/talk-draft", headers=A).status_code == 409

    r = client.post(
        f"/api/findings/{fid}/reviews",
        headers=A,
        json={"decision": "confirmed", "note": "Archive says 1972, not 1965."},
    )
    assert r.status_code == 201 and r.json()["status"] == "confirmed"

    draft = client.get(f"/api/findings/{fid}/talk-draft", headers=A).text
    assert "Special:PermanentLink/1000" in draft
    assert "Archive says 1972, not 1965." in draft
    assert "automated in 1972" in draft
    assert "DRAFT" in draft

    stats = client.get("/api/stats", headers=A).json()
    assert stats["findings"] == {"confirmed": 1}
    assert stats["reviewer_agreement"] == 1.0
    assert stats["rejected_for_unverified_quotes"] == 1
