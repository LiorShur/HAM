"""Stage 3: statements with no citation, ranked by how much they need one."""

import json

import anthropic
import httpx2
import pytest
from conftest import FIXTURES, FakeFetcher, FakeJudge, judgement
from sqlalchemy import select

from wcm import triage as triage_mod
from wcm.models import Claim, Finding, Revision, Verification
from wcm.pipeline import PARSER_VERSION, Analyzer, _uncited_priority
from wcm.triage import ClaudeTriage, StatementInput, StatementTriage, TriageResult
from wcm.wikipedia import RevisionInfo
from wcm.worker import enqueue

PAGES = {
    "https://town.example/history": "The town received its charter in 1851. "
    "In 2010 the town had 4,200 residents."
}


class TownWiki:
    lang = "en"

    def __init__(self):
        self.html_fetches = 0

    def revision_info(self, title=None, rev_id=None):
        return RevisionInfo(title="Example Town", rev_id=3000, timestamp=None)

    def revision_html(self, rev_id):
        self.html_fetches += 1
        return (FIXTURES / "parsoid_uncited.html").read_text()


CANNED = {
    "About 300": ("contentious", False, "casualty figure"),
    "The former mayor": ("contentious", True, "allegation against a named person"),
    '"The sea gave': ("quotation", False, "quotation from the harbourmaster"),
    "The town has had": ("specific", False, "date"),
    "Its population": ("specific", False, "population figure"),
    "Example Town is": ("general", False, ""),
}


class FakeTriage:
    def __init__(self):
        self.calls = []

    def triage(self, *, article_title, statements, cited_elsewhere=()):
        self.calls.append([s.text for s in statements])
        self.cited_elsewhere = [c.text for c in cited_elsewhere]
        census = next((c.statement_id for c in cited_elsewhere if "4,200" in c.text), None)
        out = {}
        for st in statements:
            cat, living, reason = next(v for k, v in CANNED.items() if st.text.startswith(k))
            out[st.statement_id] = StatementTriage(
                statement_id=st.statement_id,
                category=cat,
                living_person=living,
                reason=reason,
                covered_by=census if "4,200" in st.text else None,
            )
        return TriageResult(statements=out, model="fake-haiku")


def judge_rule(claim, sources):
    quote = "4,200 residents" if "4,200" in claim.text else "charter in 1851"
    return judgement(claim.claim_id, "supported", supporting=[quote])


def run(session, wiki=None, triage=None, recheck=False):
    triage = triage or FakeTriage()
    r = enqueue(session, "Example Town")
    Analyzer(
        session,
        wiki or TownWiki(),
        FakeFetcher(PAGES),
        FakeJudge(judge_rule),
        recheck=recheck,
        triage=triage,
    ).run(r)
    session.commit()
    return r, triage


def uncited_findings(session):
    return {
        f.claim.text[:16]: f
        for f in session.scalars(select(Finding).where(Finding.category == "uncited"))
    }


def test_uncited_statements_get_ranked_findings(session):
    r, triage = run(session)
    assert r.status == "done", r.error
    assert r.stats["claims"] == 2 and r.stats["uncited"] == 6
    # The model is shown the article's cited sentences to match against.
    assert "In 2010 the town had 4,200 residents." in triage.cited_elsewhere
    found = uncited_findings(session)
    assert found["About 300 reside"].priority == "high"
    # Contentious and about a living person: high.
    assert found["The former mayor"].priority == "high"
    # Quotations need a citation but aren't contentious: medium.
    assert found['"The sea gave us'].priority == "medium"
    # Specific facts are low (and this one is already tagged {{citation needed}}).
    assert found["The town has had"].priority == "low"
    # A lead sentence whose fact is cited in the body needs nothing (WP:LEADCITE).
    assert "Its population wa" not in found
    # General statements don't get findings.
    assert "Example Town is " not in found
    assert r.stats["uncited_findings"] == 4
    assert r.stats["uncited_covered_elsewhere"] == 1

    v = found["The former mayor"].verification
    assert v.kind == "triage" and v.severity == "contentious"
    assert v.details["living_person"] is True
    assert v.details["covered_by"] is None
    assert v.prompt_version == "triage-v2"


def test_lead_sentence_records_which_body_sentence_covers_it(session):
    run(session)
    lead = session.scalars(
        select(Verification).join(Claim).where(Claim.text.startswith("Its population"))
    ).one()
    assert lead.details["covered_by_text"] == "In 2010 the town had 4,200 residents."


def test_uncited_findings_do_not_mix_with_citation_findings(session):
    run(session)
    assert session.scalars(select(Finding).where(Finding.category == "F2")).all() == []


def test_rerun_does_not_rank_again(session):
    run(session)
    r2, triage2 = run(session)
    assert triage2.calls == []
    assert r2.stats["uncited_skipped_already_ranked"] == 6
    assert len(uncited_findings(session)) == 4


def test_recheck_ranks_again_without_duplicates(session):
    run(session)
    _, triage2 = run(session, recheck=True)
    assert len(triage2.calls) == 1
    assert len(session.scalars(select(Finding).where(Finding.category == "uncited")).all()) == 4


def test_revisions_parsed_by_older_versions_get_their_uncited_statements(session):
    # Simulate a revision stored before uncited statements were kept.
    run(session, triage=FakeTriage())
    rev = session.scalars(select(Revision)).one()
    # Delete in dependency order: findings, then their verifications, then claims.
    for f in session.scalars(select(Finding)):
        session.delete(f)
    session.flush()
    for v in session.scalars(select(Verification).where(Verification.kind == "triage")):
        session.delete(v)
    session.flush()
    for c in [c for c in rev.claims if not c.cited]:
        session.delete(c)
    rev.parser_version = 1
    session.commit()
    session.expire_all()  # a later run starts from the database, not this session's cache
    assert all(c.cited for c in session.scalars(select(Claim)))

    wiki = TownWiki()
    r, _ = run(session, wiki=wiki)
    assert wiki.html_fetches == 1  # re-parsed once
    assert session.get(Revision, rev.id).parser_version == PARSER_VERSION
    assert r.stats["uncited"] == 6
    assert len(uncited_findings(session)) == 4
    # The cited claims and their earlier results are untouched.
    assert len(session.scalars(select(Claim).where(Claim.cited.is_(True))).all()) == 2


def test_skipping_uncited(session):
    r = enqueue(session, "Example Town")
    Analyzer(session, TownWiki(), FakeFetcher(PAGES), FakeJudge(judge_rule)).run(r)
    assert r.stats["uncited"] == 6
    assert "uncited_findings" not in r.stats


@pytest.mark.parametrize(
    "category,living,lead,tagged,covered,expected",
    [
        ("contentious", False, False, False, False, "high"),
        ("contentious", False, True, False, False, "medium"),
        ("contentious", True, True, True, True, "high"),  # BLP: always high
        ("quotation", False, False, False, False, "medium"),
        ("quotation", False, True, False, False, "low"),
        ("specific", False, False, False, False, "low"),
        ("specific", False, True, True, False, "low"),
        ("specific", False, True, False, True, None),  # lead, cited in the body
        ("contentious", False, True, False, True, None),
        ("contentious", False, False, False, True, "low"),  # body: reuse the reference
        ("general", True, False, False, False, None),
    ],
)
def test_uncited_priority(category, living, lead, tagged, covered, expected):
    assert _uncited_priority(category, living, lead, tagged, covered) == expected


# --- ClaudeTriage against the real SDK, with a fake HTTP server -----------------------


def _sse(payload: dict) -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": json.dumps(payload)},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 20},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def test_claude_triage_request_and_batching(monkeypatch):
    monkeypatch.setattr(triage_mod, "BATCH_SIZE", 2)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        ids = [
            int(x.split('"')[0]) for x in body["messages"][0]["content"].split('statement id="')[1:]
        ]
        payload = {
            "statements": [
                {
                    "statement_id": i,
                    "category": "specific",
                    "living_person": False,
                    "reason": "date",
                    # 99 was never offered as a cited sentence, so it must be dropped.
                    "covered_by": 99 if i == 1 else 7,
                }
                for i in ids
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
    result = ClaudeTriage(client=client).triage(
        article_title="T",
        statements=[StatementInput(i, f"Statement {i} about something.") for i in (1, 2, 3)],
        cited_elsewhere=[StatementInput(7, "A cited sentence.")],
    )
    assert set(result.statements) == {1, 2, 3}
    # covered_by must name a sentence that was offered; anything else is dropped.
    assert result.statements[1].covered_by is None
    assert result.statements[2].covered_by == 7
    assert "A cited sentence." in bodies[0]["messages"][0]["content"]
    assert len(bodies) == 2  # batches of 2
    b = bodies[0]
    assert b["model"] == "claude-haiku-5-5"
    assert b["output_config"]["effort"] == "low"
    assert b["stream"] is True
    assert "fallbacks" not in b and "temperature" not in b
    assert result.usage["input_tokens"] == 20
