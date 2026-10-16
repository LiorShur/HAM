from pathlib import Path

from conftest import FakeJudge, judgement

from wcm.evaluation import run

GOLD = Path(__file__).parents[1] / "evals" / "gold.jsonl"


def test_eval_harness_runs_on_the_gold_set():
    # A judge that says "not supported" for everything: recall 1, precision < 1.
    report = run(GOLD, judge=FakeJudge(lambda c, s: judgement(c.claim_id, "not_supported")))
    assert report["cases"] == 6
    assert report["flag_recall"] == 1.0
    assert report["false_flags"] > 0
    assert report["judge_errors"] == 0
