from datetime import UTC, datetime

from test_api import A, client  # noqa: F401  (fixture)

from wcm import api
from wcm.discovery import Candidate, DiscoveryResult
from wcm.models import AnalysisRun, RunStatus


def fake_discovery(topic, lang):
    assert topic == "Example River floods" and lang == "en"
    return DiscoveryResult(
        candidates=[
            Candidate(
                "Great Flood of 1862",
                "search",
                "Flood in Example County",
                30000,
                "extendedconfirmed",
                datetime(2026, 10, 1, tzinfo=UTC),
                3,
                "core event",
            ),
            Candidate("Example River", "search", "River", 15000, None, None, 2, "context"),
            Candidate(
                "Example Relief Society",
                "Category:Relief",
                "Charity",
                1500,
                None,
                None,
                1,
                "tangential",
            ),
        ],
        model="claude-haiku-5-5",
        usage={"input_tokens": 500},
        sources={"search": 2, "Category:Relief": 1},
    )


def make(client):  # noqa: F811
    api.app.dependency_overrides[api.get_discovery] = lambda: fake_discovery
    r = client.post("/api/collections", json={"topic": "Example River floods"}, headers=A)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_collection_lists_ranked_candidates_with_estimates(client):  # noqa: F811
    c = make(client)
    assert c["topic"] == "Example River floods"
    assert c["article_count"] == 3 and c["selected_count"] == 0
    titles = [a["title"] for a in c["articles"]]
    assert titles == ["Great Flood of 1862", "Example River", "Example Relief Society"]
    flood = c["articles"][0]
    assert flood["edit_protection"] == "extendedconfirmed"
    assert flood["url"] == "https://en.wikipedia.org/wiki/Great_Flood_of_1862"
    # No finished runs yet: the default per-citation cost; 30,000 bytes ~ 20 citations.
    assert c["cost_basis_runs"] == 0
    assert flood["estimated_cost_usd"] == round(20 * c["cost_per_citation"], 2)
    assert flood["last_run"] is None
    listed = client.get("/api/collections", headers=A).json()
    assert [x["id"] for x in listed] == [c["id"]]


def test_scan_queues_selected_articles_once(client, session):  # noqa: F811
    c = make(client)
    url = f"/api/collections/{c['id']}/scan"
    r = client.post(url, json={"titles": ["Great Flood of 1862", "Example River"]}, headers=A)
    assert r.status_code == 201
    assert r.json()["queued"] == 2
    # Pressing again doesn't queue duplicates of runs still waiting.
    again = client.post(url, json={"titles": ["Great Flood of 1862"]}, headers=A).json()
    assert again["queued"] == 0 and again["already_in_progress"] == ["Great Flood of 1862"]

    runs = session.query(AnalysisRun).all()
    assert {r.title for r in runs} == {"Great Flood of 1862", "Example River"}
    assert all(r.collection_id == c["id"] and r.requested_by == "alice" for r in runs)

    detail = client.get(f"/api/collections/{c['id']}", headers=A).json()
    assert detail["selected_count"] == 2
    flood = detail["articles"][0]
    assert flood["selected"] and flood["last_run"]["status"] == "queued"

    # Once finished, it can be queued again.
    for r in runs:
        r.status = RunStatus.done
    session.commit()
    third = client.post(url, json={"titles": ["Great Flood of 1862"]}, headers=A).json()
    assert third["queued"] == 1


def test_scan_rejects_titles_not_in_the_collection(client):  # noqa: F811
    c = make(client)
    r = client.post(
        f"/api/collections/{c['id']}/scan", json={"titles": ["Something else"]}, headers=A
    )
    assert r.status_code == 422


def test_discovery_errors_are_reported(client):  # noqa: F811
    from wcm.judge import JudgeConfigError

    def no_key(topic, lang):
        raise JudgeConfigError("No Anthropic API key found.")

    api.app.dependency_overrides[api.get_discovery] = lambda: no_key
    r = client.post("/api/collections", json={"topic": "Example River floods"}, headers=A)
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_runs_report_cost(client, session):  # noqa: F811
    from test_pipeline import run_pipeline

    run_pipeline(session)
    runs = client.get("/api/runs", headers=A).json()
    assert "cost_usd" in runs[0] and runs[0]["collection_id"] is None
