"""What runs cost, from the token usage stored with each verification.

Prices are USD per million tokens (Anthropic API list prices, October 2026). Check
https://platform.claude.com/docs/en/about-claude/pricing if they look out of date.
"""

import json
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from wcm.models import AnalysisRun, Verification

# model: (input, output, cache read) per million tokens.
# Cache writes are billed at 1.25x input (5-minute cache).
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    # Haiku 5.5's rate for prompts up to 100K tokens; our triage prompts are smaller.
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

# Used for estimates until there are finished runs to learn from: about what one
# web citation cost on the first real article checked (Cape Hatteras Lighthouse).
DEFAULT_COST_PER_CITATION = 0.08


def usage_cost(model: str | None, usage: dict) -> float:
    price = PRICES.get(model or "")
    if not price or not usage:
        return 0.0
    inp, out, cache_read = price
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
        + usage.get("cache_read_input_tokens", 0) * cache_read
        + usage.get("output_tokens", 0) * out
    ) / 1_000_000


def verifications_cost(verifications: Iterable[Verification]) -> float:
    """Sum usage once per API call. One call can judge several claims, and each of
    their verifications carries a copy of that call's usage."""
    seen: set[str] = set()
    total = 0.0
    for v in verifications:
        if not v.usage:
            continue
        key = v.request_id or json.dumps(v.usage, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        total += usage_cost(v.served_model or v.model, v.usage)
    return total


def run_cost(session: Session, run_id: int) -> float:
    rows = session.scalars(select(Verification).where(Verification.run_id == run_id))
    return round(verifications_cost(rows), 4)


def cost_per_citation(session: Session) -> tuple[float, int]:
    """Average cost of checking one citation, learned from finished runs.
    Returns (dollars, number of runs it's based on)."""
    total, citations, runs = 0.0, 0, 0
    for run in session.scalars(select(AnalysisRun).where(AnalysisRun.status == "done")):
        checked = (run.stats or {}).get("citations_checked", 0)
        if not checked:
            continue
        cost = run_cost(session, run.id)
        if cost <= 0:
            continue
        total += cost
        citations += checked
        runs += 1
    if not citations:
        return DEFAULT_COST_PER_CITATION, 0
    return total / citations, runs
