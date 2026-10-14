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
) -> None:
    """Analyse one article now, in this process."""
    from wcm.worker import enqueue, process

    if not title and rev is None:
        raise typer.BadParameter("Give a title or --rev")
    with session_scope() as session:
        run = enqueue(session, title, rev, lang, requested_by="cli")
        session.commit()
        process(session, run)
        typer.echo(f"Run {run.id}: {run.status}")
        typer.echo(json.dumps(run.stats, indent=2))
        if run.error:
            typer.echo(run.error, err=True)
            raise typer.Exit(1)


@app.command("demo-seed")
def demo_seed() -> None:
    """Load a synthetic article with canned verdicts (no network or API key needed)."""
    from wcm.demo import seed

    with session_scope() as session:
        run_id = seed(session)
    typer.echo(f"Seeded demo run {run_id}. Start the server and open the review queue.")


@app.command()
def worker(poll: float = typer.Option(5.0, help="Seconds between queue polls")) -> None:
    """Process queued runs forever."""
    from wcm.worker import work_forever

    work_forever(poll)


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

    report = run_gold(path, limit=limit)
    typer.echo(json.dumps(report, indent=2))


if __name__ == "__main__":
    app()
