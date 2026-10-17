# HAM: Historical Accuracy Monitor

Python 3.12+ (FastAPI, SQLAlchemy 2, Alembic) backend in `src/wcm`, React + TS UI
in `web/`. See README.md for the pipeline and docs/ROADMAP.md for the phases.

## Commands

- Tests: `pytest -q` (SQLite). For Postgres, also set `TEST_DATABASE_URL`.
- Lint: `ruff check src tests`; format: `ruff format src tests`
- UI: `cd web && npm run build` (type-checks too)
- After changing `src/wcm/models.py`: `alembic revision --autogenerate -m "..."`
  against Postgres, and check the generated file.

## Rules for this codebase

- Read-only toward Wikipedia. Never add code that edits Wikipedia or posts to it.
- The judge must quote the source; `pipeline.check_quotes` rejects quotes that
  aren't in it. Don't weaken that check to get more findings.
- When you change `judge.SYSTEM_PROMPT` or the output schema, bump
  `PROMPT_VERSION` so stored verdicts stay traceable.
- Claude calls go through the official `anthropic` SDK only. The judge uses
  `claude-opus-5-5` with the `fallbacks="default"` refusal fallback; ranking uncited
  statements uses `claude-haiku-5-5` at low effort (no fallback available).
- When you change `triage.SYSTEM_PROMPT` or its schema, bump `TRIAGE_VERSION`.
- When you change how claims are extracted, bump `pipeline.PARSER_VERSION` if
  stored revisions need re-parsing.
- Analyse content, not editors: no features that profile or track Wikipedia users.
- Test fixtures and examples use synthetic content, never real allegations.
