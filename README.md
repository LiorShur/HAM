# HAM: Historical Accuracy Monitor

Checks whether Wikipedia citations actually support the sentences they are attached
to. A person reviews every flagged issue before anything is raised on Wikipedia.

This is **Phase 1** of the plan in [docs/ROADMAP.md](docs/ROADMAP.md): citation
verification (finding type F2, "the cited source does not support the claim").
It is read-only. It never edits Wikipedia.

Read [docs/WIKIPEDIA_CONDUCT.md](docs/WIKIPEDIA_CONDUCT.md) before anyone uses
this on real articles, especially in the Arab–Israeli conflict topic area.

## How it works

```
article title or revision id
  → Wikipedia: exact revision, Parsoid HTML           (wcm/wikipedia.py)
  → claims = sentences carrying footnotes,
    each linked to its citations                      (wcm/parsing.py)
  → fetch each cited source: live URL, else the
    archive link, else the Wayback Machine            (wcm/sources.py)
  → stage 1, per source: Claude judges every claim
    that cites it and must quote the passages it
    relies on                                         (wcm/judge.py)
  → quotes are checked by string matching; a verdict
    with a made-up quote is thrown out                (wcm/pipeline.py)
  → a negative verdict on a live page is re-checked
    against an archived copy (the page may have
    changed since it was cited)
  → stage 2, per claim: if any source supports it,
    it's fine; if several sources each cover part,
    Claude judges them together
  → at most one finding per claim goes to the
    review queue                                      (wcm/api.py, web/)
  → a confirmed finding gets a talk-page draft that
    the reviewer rewrites in their own words          (wcm/reports.py)
```

Verdicts: `supported`, `partially_supported`, `not_supported`, `contradicted`,
`source_insufficient` (paywall, abstract only, wrong page), and
`source_unavailable` (no URL, or fetch failed). Only the first four come from the
model. A claim gets a finding when its sources, taken together, only partly
support it, don't support it, or contradict it, and only when every quote was
found in the source. Findings are marked:

- **minor** (low priority) when the difference wouldn't change what a reader
  understands, e.g. 198 vs 200 feet. Disputed or central facts (casualty figures,
  whether an event took place, who was responsible) are never minor.
- **check offline source** (low priority) when the readable sources don't cover
  everything but the claim also cites a source that can't be read online, such as
  a book.

When the prompt changes (`PROMPT_VERSION` in `wcm/judge.py`), re-running an
article re-judges it, and any finding nobody has decided on yet is marked
*superseded* by the new assessment. Confirmed and dismissed findings are kept.

Everything is stored for audit: revision id, the citation as written, source URL,
fetch time and content hash, model, served model, prompt version, the request id,
and every reviewer decision.

## Claude settings

The judge uses **Claude Opus 5.5** (`claude-opus-5-5`) at `high` effort with
structured outputs (`wcm/judge.py`). It also turns on Anthropic's **server-side
refusal fallback** (`fallbacks="default"`): if a safety classifier declines a
request, the API retries it on Anthropic's recommended fallback model. That can
happen with sources on violent events. The model that actually answered is
stored in `verifications.served_model`. Set `WCM_JUDGE_MODEL` / `WCM_JUDGE_EFFORT`
to change these.

Cost: each cited source is sent once, together with every claim in the revision
that cites it. Sources longer than `WCM_MAX_SOURCE_CHARS` are trimmed to the
passages that share the most words with the claims. Expect roughly one judge call
per source. Measure the cost on a few articles before checking many.

## Quick start (local)

Requires Python 3.12+ and Node 22.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set ANTHROPIC_API_KEY, WCM_CONTACT, WCM_REVIEWER_TOKENS
(cd web && npm ci && npm run build)

wcm init-db                     # creates or upgrades the database; run after every git pull
wcm demo-seed                   # optional: synthetic article, no API key needed
wcm serve                       # http://127.0.0.1:8000, sign in with a reviewer token
wcm worker                      # in another terminal: processes queued checks
```

You can also check an article straight from the command line:

```bash
wcm scan "Article title"
wcm scan --rev 1234567890
```

## With Docker (Postgres)

```bash
cp .env.example .env            # fill it in
docker compose up --build       # db, migrations, api on :8000, worker
```

## Development

```bash
pytest -q                                   # SQLite
TEST_DATABASE_URL=postgresql+psycopg://wcm:wcm@localhost/wcm_test pytest -q
ruff check src tests
alembic revision --autogenerate -m "..."    # after changing wcm/models.py
cd web && npm run dev                       # UI dev server, proxies /api to :8000
```

## Measuring accuracy

`evals/gold.jsonl` holds labelled cases (claim, source text, expected verdict).
It currently has six **synthetic** examples that only show the format. Phase 0 is
to replace them with 200–500 real, hand-labelled claims from different parts of
the topic, labelled with `perspective` so the report shows flag rates per side.

```bash
wcm eval evals/gold.jsonl       # calls the Claude API, so it costs money
```

The report includes flag precision, recall, false flags, how often the model
quoted text that wasn't in the source, and flag rates per `perspective`. Don't
post anything on Wikipedia until precision is high on real data.

## Layout

```
src/wcm/      config, models, wikipedia, parsing, sources, judge, pipeline,
              worker, api, reports, evaluation, demo, cli
alembic/      database migrations
web/          React + TypeScript review UI (Vite)
tests/        pytest, with a synthetic Parsoid fixture
evals/        gold set for the judge
docs/         roadmap, conduct rules
```
