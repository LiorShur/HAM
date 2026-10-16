"""Score the judge against a hand-labelled gold set.

Each line of the JSONL file is one case:
  {"id": "...", "claim": "...", "context_before": "", "citation_text": "...",
   "source_url": "...", "source_text": "...", "expected": "<verdict>",
   "perspective": "<optional label>", "notes": "..."}

`perspective` is a free label (e.g. which side a claim favours). The report shows
flag rates per label, so a skew in either direction is visible.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

from wcm.judge import ClaimInput, ClaudeJudge, Judge, JudgeError, SourceInput
from wcm.pipeline import FINDING_VERDICTS, check_quotes


def load(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def run(path: str | Path, judge: Judge | None = None, limit: int | None = None) -> dict:
    judge = judge or ClaudeJudge()
    cases = load(path)[:limit] if limit else load(path)
    confusion: Counter = Counter()
    per_perspective: dict[str, Counter] = defaultdict(Counter)
    errors, bad_quotes, rows = 0, 0, []

    for i, case in enumerate(cases):
        try:
            result = judge.judge(
                sources=[
                    SourceInput(
                        source_id=1,
                        citation_text=case.get("citation_text", ""),
                        url=case.get("source_url", ""),
                        text=case["source_text"],
                    )
                ],
                claims=[ClaimInput(i, case["claim"], case.get("context_before", ""))],
            )
        except JudgeError as exc:
            errors += 1
            rows.append({"id": case.get("id"), "error": str(exc)})
            continue
        j = result.judgements[i]
        _, quotes_ok = check_quotes(j, {1: (None, case["source_text"])}, 0)
        bad_quotes += not quotes_ok
        flagged = quotes_ok and j.verdict in FINDING_VERDICTS
        should_flag = case["expected"] in FINDING_VERDICTS
        confusion[(case["expected"], j.verdict)] += 1
        p = per_perspective[case.get("perspective", "unlabelled")]
        p["cases"] += 1
        p["flagged"] += flagged
        p["false_flags"] += flagged and not should_flag
        rows.append(
            {
                "id": case.get("id"),
                "expected": case["expected"],
                "got": j.verdict,
                "flagged": flagged,
                "quotes_ok": quotes_ok,
                "confidence": j.confidence,
            }
        )

    tp = sum(1 for r in rows if r.get("flagged") and r["expected"] in FINDING_VERDICTS)
    fp = sum(1 for r in rows if r.get("flagged") and r["expected"] not in FINDING_VERDICTS)
    fn = sum(
        1 for r in rows if "got" in r and not r["flagged"] and r["expected"] in FINDING_VERDICTS
    )
    exact = sum(1 for r in rows if r.get("got") == r.get("expected"))
    judged = sum(1 for r in rows if "got" in r)
    return {
        "cases": len(cases),
        "judge_errors": errors,
        "exact_verdict_accuracy": round(exact / judged, 3) if judged else None,
        "flag_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "flag_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "false_flags": fp,
        "unverified_quote_rate": round(bad_quotes / judged, 3) if judged else None,
        "by_perspective": {k: dict(v) for k, v in per_perspective.items()},
        "confusion": {f"{e} -> {g}": n for (e, g), n in sorted(confusion.items())},
        "rows": rows,
    }
