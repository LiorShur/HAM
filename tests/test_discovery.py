"""Topic discovery against a fake Wikipedia API (synthetic topic and titles)."""

import httpx
import respx

from wcm.discovery import ArticleRating, discover
from wcm.wikipedia import WikipediaClient

API = "https://en.wikipedia.org/w/api.php"
TOPIC = "Example River floods"

SEARCH = [
    "Great Flood of 1862",
    "Example River",
    "Flood control in Example County",
    "Example River (disambiguation)",
]
CATEGORIES = ["Category:Example River floods"]
CATEGORY_PAGES = {
    "Category:Example River floods": (
        ["Great Flood of 1862", "1911 Example River flood"],
        ["Category:Flood relief in Example County"],
    ),
    "Category:Flood relief in Example County": (["Example Relief Society"], []),
}
LINKS = ["Example County", "Rainfall", "Great Flood of 1862"]
INFO = {
    TOPIC: {"desc": "Overview of floods on the Example River", "size": 40000},
    "Great Flood of 1862": {
        "desc": "Flood in Example County",
        "size": 30000,
        "protection": "extendedconfirmed",
    },
    "Example River": {"desc": "River in Example County", "size": 20000},
    "Flood control in Example County": {"desc": "Levees and dams", "size": 9000},
    "Example River (disambiguation)": {"desc": "", "size": 500, "disambig": True},
    "1911 Example River flood": {"desc": "Flood in 1911", "size": 12000},
    "Example Relief Society": {"desc": "Charity founded after the 1862 flood", "size": 5000},
    "Example County": {"desc": "County", "size": 50000},
    "Rainfall": {"desc": "Liquid precipitation", "size": 60000},
}


def handler(request):
    p = dict(request.url.params)
    if p.get("list") == "search":
        titles = CATEGORIES if p["srnamespace"] == "14" else SEARCH
        return httpx.Response(200, json={"query": {"search": [{"title": t} for t in titles]}})
    if p.get("list") == "categorymembers":
        pages, subs = CATEGORY_PAGES.get(p["cmtitle"], ([], []))
        members = [{"title": t, "ns": 0} for t in pages] + [{"title": t, "ns": 14} for t in subs]
        return httpx.Response(200, json={"query": {"categorymembers": members}})
    if p.get("prop") == "links":
        return httpx.Response(
            200,
            json={
                "query": {"pages": [{"title": p["titles"], "links": [{"title": t} for t in LINKS]}]}
            },
        )
    if p.get("prop") == "info|pageprops|revisions":
        pages = []
        for t in p["titles"].split("|"):
            meta = INFO.get(t)
            if meta is None:
                pages.append({"title": t, "missing": True})
                continue
            props = {"wikibase-shortdesc": meta["desc"]}
            if meta.get("disambig"):
                props["disambiguation"] = ""
            protection = (
                [{"type": "edit", "level": meta["protection"]}] if "protection" in meta else []
            )
            pages.append(
                {
                    "title": t,
                    "ns": 0,
                    "length": meta["size"],
                    "pageprops": props,
                    "protection": protection,
                    "revisions": [{"timestamp": "2026-10-01T12:00:00Z"}],
                }
            )
        return httpx.Response(200, json={"query": {"pages": pages}})
    raise AssertionError(f"unexpected request {p}")


class FakeRanker:
    def __init__(self):
        self.seen = []

    def rate(self, topic, articles):
        self.seen = articles
        scores = {
            "Great Flood of 1862": 3,
            "1911 Example River flood": 3,
            TOPIC: 3,
            "Example River": 2,
            "Flood control in Example County": 2,
            "Example Relief Society": 1,
            "Example County": 1,
            "Rainfall": 0,
        }
        return {
            i: ArticleRating(article_id=i, relevance=scores[t], note="n") for i, t, _ in articles
        }, {"input_tokens": 100, "output_tokens": 10}


@respx.mock
def test_discovery_gathers_from_all_sources_and_ranks():
    respx.get(API).mock(side_effect=handler)
    wiki = WikipediaClient(lang="en", client=httpx.Client())
    ranker = FakeRanker()
    result = discover(wiki, ranker, TOPIC)
    titles = [c.title for c in result.candidates]

    # Disambiguation pages dropped; duplicates (found by search, category and link) merged.
    assert "Example River (disambiguation)" not in titles
    assert titles.count("Great Flood of 1862") == 1
    assert len(ranker.seen) == 8
    # Unrelated articles (relevance 0) are left out; most relevant first.
    assert "Rainfall" not in titles
    assert [c.relevance for c in result.candidates] == sorted(
        [c.relevance for c in result.candidates], reverse=True
    )
    # Within a relevance level, Wikipedia's order is kept: the topic article first.
    assert titles[0] == TOPIC

    by_title = {c.title: c for c in result.candidates}
    assert by_title["Great Flood of 1862"].found_via == "search"
    assert by_title["1911 Example River flood"].found_via == "Category:Example River floods"
    assert by_title["Example Relief Society"].found_via == "Category:Flood relief in Example County"
    assert by_title["Example County"].found_via == f"linked from {TOPIC}"
    assert by_title["Great Flood of 1862"].edit_protection == "extendedconfirmed"
    assert by_title["Great Flood of 1862"].description == "Flood in Example County"
    assert by_title["Great Flood of 1862"].size == 30000
    assert result.usage == {"input_tokens": 100, "output_tokens": 10}


def test_claude_ranker_request():
    import json

    import anthropic
    import httpx2
    from test_uncited import _sse

    from wcm.discovery import ClaudeRanker

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        payload = {
            "ratings": [
                {"article_id": 0, "relevance": 3, "note": "core"},
                {"article_id": 1, "relevance": 0, "note": "unrelated"},
            ]
        }
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(payload)
        )

    client = anthropic.Anthropic(
        api_key="test",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    ratings, usage = ClaudeRanker(client=client).rate(
        TOPIC, [(0, "Great Flood of 1862", "Flood"), (1, "Rainfall", "Weather")]
    )
    assert ratings[0].relevance == 3 and ratings[1].relevance == 0
    assert bodies[0]["model"] == "claude-haiku-5-5"
    assert bodies[0]["output_config"]["effort"] == "low"
    assert "Great Flood of 1862" in bodies[0]["messages"][0]["content"]
    assert usage["input_tokens"] == 10
