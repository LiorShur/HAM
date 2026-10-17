import pytest
from sqlalchemy import select
from test_pipeline import run_pipeline

from wcm.costs import (
    DEFAULT_COST_PER_CITATION,
    cost_per_citation,
    run_cost,
    usage_cost,
    verifications_cost,
)
from wcm.models import Verification


def test_usage_cost_uses_model_prices():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000}
    assert usage_cost("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0)
    assert usage_cost("claude-haiku-5-5", usage) == pytest.approx(0.10 + 0.05)
    cached = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert usage_cost("claude-opus-5-5", cached) == pytest.approx(0.20 + 5.0)
    assert usage_cost("unknown-model", usage) == 0.0


def test_one_call_judging_several_claims_is_counted_once():
    shared = {"input_tokens": 10_000, "output_tokens": 1_000}
    vs = [
        Verification(model="claude-opus-5-5", request_id="req_1", usage=shared),
        Verification(model="claude-opus-5-5", request_id="req_1", usage=shared),
        Verification(model="claude-opus-5-5", request_id="req_2", usage=shared),
        # Older rows have no request id: identical usage means the same call.
        Verification(model="claude-opus-5-5", request_id=None, usage={"input_tokens": 5}),
        Verification(model="claude-opus-5-5", request_id=None, usage={"input_tokens": 5}),
    ]
    one = usage_cost("claude-opus-5-5", shared)
    assert verifications_cost(vs) == pytest.approx(
        2 * one + usage_cost("claude-opus-5-5", {"input_tokens": 5})
    )


def test_served_model_is_priced_when_a_fallback_answered():
    v = Verification(
        model="claude-opus-5-5",
        served_model="claude-opus-5",
        request_id="r",
        usage={"output_tokens": 1_000_000},
    )
    assert verifications_cost([v]) == pytest.approx(25.0)


def test_run_cost_and_learned_cost_per_citation(session):
    assert cost_per_citation(session) == (DEFAULT_COST_PER_CITATION, 0)
    run, _, _ = run_pipeline(session)
    for i, v in enumerate(session.scalars(select(Verification))):
        v.model = v.served_model = "claude-opus-5-5"
        v.request_id = f"req_{i}"
        v.usage = {"input_tokens": 100_000, "output_tokens": 0}  # $0.40 each
    session.commit()
    n = len([v for v in session.scalars(select(Verification))])
    assert run_cost(session, run.id) == pytest.approx(0.40 * n)
    per, runs = cost_per_citation(session)
    assert runs == 1
    assert per == pytest.approx(0.40 * n / run.stats["citations_checked"])
