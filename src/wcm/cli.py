import json
import logging

import typer

from wcm.db import get_engine, session_scope
from wcm.models import Base

app = typer.Typer(help="Wiki Citation Monitor", no_args_is_help=True)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("init-db")
def init_db() -> None:
    """Create tables directly (local SQLite/dev). Use `alembic upgrade head` on Postgres."""
    Base.metadata.create_all(get_engine())
    typer.echo("Tables created.")


@app.command()
def scan(
    title: str = typer.Argument(None, help="Article title"),
    rev: int = typer.Option(None, help="Exact revision id"),
    lang: str = typer.Option("en"),
    recheck: bool = typer.Option(
        False, help="Judge every citation again, even ones already checked for this revision"
    ),
) -> None:
    """Analyse one article now, in this process."""
    from wcm.judge import ClaudeJudge
    from wcm.models import RunStatus
    from wcm.worker import enqueue, process

    if not title and rev is None:
        raise typer.BadParameter("Give a title or --rev")
    judge = _make_judge(ClaudeJudge)
    with session_scope() as session:
        run = enqueue(session, title, rev, lang, requested_by="cli")
        # Mark it as taken before committing, so a running `wcm worker` doesn't
        # pick up the same run.
        run.status = RunStatus.running
        session.commit()
        process(session, run, judge=judge, recheck=recheck)
        typer.echo(f"Run {run.id}: {run.status}")
        typer.echo(json.dumps(run.stats, indent=2))
        if run.error:
            typer.echo(run.error, err=True)
        if run.status == RunStatus.failed:
            raise typer.Exit(1)


@app.command("demo-seed")
def demo_seed() -> None:
    """Load a synthetic article with canned verdicts (no network or API key needed)."""
    from wcm.demo import seed

    with session_scope() as session:
        run_id = seed(session)
    typer.echo(f"Seeded demo run {run_id}. Start the server and open the review queue.")


def _make_judge(factory):
    from wcm.judge import JudgeConfigError

    try:
        return factory()
    except JudgeConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def worker(poll: float = typer.Option(5.0, help="Seconds between queue polls")) -> None:
    """Process queued runs forever."""
    from wcm.judge import ClaudeJudge
    from wcm.worker import work_forever

    work_forever(poll, judge=_make_judge(ClaudeJudge))


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the API (and the built web UI, if web/dist exists)."""
    import uvicorn

    uvicorn.run("wcm.api:app", host=host, port=port, reload=reload)


@app.command("eval")
def run_eval(
    path: str = typer.Argument("evals/gold.jsonl"),
    limit: int = typer.Option(None, help="Only the first N cases"),
) -> None:
    """Score the judge against a labelled gold set. Calls the Claude API (costs money)."""
    from wcm.evaluation import run as run_gold
    from wcm.judge import ClaudeJudge

    report = run_gold(path, judge=_make_judge(ClaudeJudge), limit=limit)
    typer.echo(json.dumps(report, indent=2))


if __name__ == "__main__":
    app()
