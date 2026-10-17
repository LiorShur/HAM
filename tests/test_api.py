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
    assert detail["assessment"] == "source"
    [src] = detail["sources"]
    assert src["source_via_archive"] is True
    assert src["verdict"] == "contradicted"
    assert detail["quotes"][0]["source_number"] == 1

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


def test_multi_source_finding_detail_and_draft(client, session):
    from test_claims import run as run_multi

    run_multi(session)
    session.commit()
    findings = client.get("/api/findings?status=open", headers=A).json()
    by_claim = {f["claim"][:20]: f for f in findings}
    assert set(by_claim) == {
        "The light was visibl",
        "The keeper's house w",
        "Its height is 61 met",
    }
    keeper = by_claim["The keeper's house w"]
    assert keeper["needs_offline_check"] is True and keeper["priority"] == "low"
    assert by_claim["Its height is 61 met"]["severity"] == "minor"

    fid = by_claim["The light was visibl"]["id"]
    detail = client.get(f"/api/findings/{fid}", headers=A).json()
    assert detail["assessment"] == "claim"
    assert [s["number"] for s in detail["sources"]] == [1, 2]
    assert {s["verdict"] for s in detail["sources"]} == {"not_supported"}

    client.post(
        f"/api/findings/{fid}/reviews",
        headers=A,
        json={"decision": "confirmed", "note": "Neither source gives a range."},
    )
    draft = client.get(f"/api/findings/{fid}/talk-draft", headers=A).text
    assert "This cites 2 sources:" in draft
    assert draft.count("does not appear to say this") == 2
    assert "Neither source gives a range." in draft

    kid = keeper["id"]
    client.post(f"/api/findings/{kid}/reviews", headers=A, json={"decision": "confirmed"})
    draft = client.get(f"/api/findings/{kid}/talk-draft", headers=A).text
    assert "I could not check it online" in draft
    assert "may cover the rest" in draft


def test_uncited_findings_in_api(client, session):
    from test_uncited import run as run_town

    run_town(session)
    session.commit()
    uncited = client.get("/api/findings?category=uncited", headers=A).json()
    assert [f["priority"] for f in uncited] == ["high", "high", "low"]  # high first
    assert all(f["category"] == "uncited" for f in uncited)
    assert client.get("/api/findings?category=F2", headers=A).json() == []

    mayor = next(f for f in uncited if f["claim"].startswith("The former mayor"))
    detail = client.get(f"/api/findings/{mayor['id']}", headers=A).json()
    assert detail["assessment"] == "triage"
    assert detail["severity"] == "contentious"
    assert detail["details"]["living_person"] is True
    assert detail["sources"] == []

    client.post(
        f"/api/findings/{mayor['id']}/reviews",
        headers=A,
        json={"decision": "confirmed", "note": "No source anywhere in the article."},
    )
    draft = client.get(f"/api/findings/{mayor['id']}/talk-draft", headers=A).text
    assert "Unsourced statement" in draft
    assert "[[WP:BLP]]" in draft
    assert "No source anywhere in the article." in draft
