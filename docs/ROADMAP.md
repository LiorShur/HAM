# Roadmap

## Phase 0: ground truth (do this alongside Phase 1)

- [ ] Choose about 10 articles from different parts of the topic area.
- [ ] Check their citations by hand and record the results in `evals/gold.jsonl`
      (200–500 claims), with a `perspective` label on each.
- [ ] Run `wcm eval` and set a precision target before anything is posted.

## Phase 1: citation verification (built)

- [x] Fetch an exact revision (Parsoid HTML) and extract footnoted claims.
- [x] Fetch sources: live URL, citation archive link, Wayback Machine; HTML and PDF.
- [x] Claude judge with structured output, required verbatim quotes, and a
      server-side refusal fallback.
- [x] Mechanical quote check; drop verdicts that quote text not in the source.
- [x] Re-check negative verdicts against an archived copy.
- [x] Review queue, reviewer decisions, audit trail, talk-page draft for confirmed findings.
- [x] Eval harness (precision, recall, flag rate per perspective).
- [x] First real article (Cape Hatteras Lighthouse): most false alarms came from
      judging each sentence–source pair separately. Now one assessment per claim,
      sources judged together, offline sources flagged, minor differences marked.
- [x] Uncited statements: found by position relative to footnotes, ranked by
      Claude Haiku 5.5 (contentious / specific / general) without judging truth.
- [ ] Try on real articles once there's network access to Wikipedia, and tune
      the claim extraction (lists, tables, quotes).
- [ ] Books: page-level lookup through Internet Archive / Google Books where a
      preview exists; until then, books without a URL are reported as unavailable.
- [ ] Better reviewer accounts (per-user passwords or SSO) instead of shared tokens.
- [ ] Batch API for bulk runs (half price, not time-sensitive).

## Phase 2: knowledge base and contradiction checks (F1, F5)

- Tables for sources, passages and claims, where each claim has a status:
  established / majority view / disputed (with the competing positions and who
  holds them) / fringe.
- Embeddings with pgvector to find knowledge-base passages related to an article claim.
- F1: article claim conflicts with strong, independent evidence.
- F5: a disputed view stated in Wikipedia's own voice without attribution.
- Hebrew and Arabic sources from the start.
- Avoid circular evidence: drop sources that copy Wikipedia.

## Phase 3: monitoring

- Watch tracked articles through Wikimedia EventStreams (recent changes).
- When a new revision adds or changes cited text, check only the changed claims.
- Show findings across all tracked articles per perspective; alert reviewers.

## Phase 4: source reliability (F3)

- Load WP:RSP (perennial sources) status per domain and keep it updated.
- Flag citations to deprecated or generally unreliable sources, and primary
  sources used to support interpretation.

## Later

- Other topics and languages (Hebrew and Arabic Wikipedia have their own
  communities and rules).
- Publish the method and the aggregate statistics.
