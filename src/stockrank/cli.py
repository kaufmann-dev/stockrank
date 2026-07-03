from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from . import __version__
from .config import ConfigError, available_names, load_global_config, resolve_strategy
from .finalize import finalize_portfolio, finalize_proposals, finalize_scores
from .live import commit_run
from .prepare import prepare_portfolio, prepare_proposals, prepare_scores
from .tracking import track_all, track_live, track_run


console = Console()
app = typer.Typer(no_args_is_help=True, help="stockrank external-agent stock ranking workflow")
prepare_app = typer.Typer(no_args_is_help=True)
finalize_app = typer.Typer(no_args_is_help=True)
app.add_typer(prepare_app, name="prepare")
app.add_typer(finalize_app, name="finalize")


def _root() -> Path:
    return Path.cwd()


def _fail(exc: Exception) -> None:
    console.print(f"[red]error:[/] {exc}")
    raise typer.Exit(1)


def choose_strategy(root: Path) -> str:
    config = load_global_config(root)
    strategies = available_names(root, "strategies")
    if not strategies:
        raise ConfigError("no strategies/*.toml files found")
    default_index = strategies.index(config.default_strategy) + 1 if config.default_strategy in strategies else 1
    table = Table(title="Strategies")
    table.add_column("#", justify="right")
    table.add_column("name")
    for index, name in enumerate(strategies, start=1):
        table.add_row(str(index), f"{name} (default)" if index == default_index else name)
    console.print(table)
    answer = Prompt.ask("Strategy", default=str(default_index))
    try:
        selected = int(answer)
    except ValueError as exc:
        raise ConfigError("strategy selection must be a number") from exc
    if selected < 1 or selected > len(strategies):
        raise ConfigError(f"strategy selection must be between 1 and {len(strategies)}")
    return strategies[selected - 1]


@prepare_app.command("scores")
def prepare_scores_cmd(
    strategy: Annotated[str | None, typer.Option("--strategy", help="Strategy name from strategies/*.toml")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing phase work folder")] = False,
) -> None:
    try:
        selected = strategy or choose_strategy(_root())
        prepare_scores(_root(), selected, force=force, console=console)
    except Exception as exc:
        _fail(exc)


@prepare_app.command("proposals")
def prepare_proposals_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest run with finalized scores")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing phase work folder")] = False,
) -> None:
    try:
        selected = prepare_proposals(_root(), run_id=run_id, force=force)
        console.print(f"Prepared proposals for run {selected}")
    except Exception as exc:
        _fail(exc)


@prepare_app.command("portfolio")
def prepare_portfolio_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest run with finalized proposals")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing phase work folder")] = False,
) -> None:
    try:
        selected = prepare_portfolio(_root(), run_id=run_id, force=force)
        console.print(f"Prepared portfolio for run {selected}")
    except Exception as exc:
        _fail(exc)


@finalize_app.command("scores")
def finalize_scores_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest run")] = None,
) -> None:
    try:
        selected = finalize_scores(_root(), run_id=run_id, console=console)
        console.print(f"Finalized scores for run {selected}")
    except Exception as exc:
        _fail(exc)


@finalize_app.command("proposals")
def finalize_proposals_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest run")] = None,
) -> None:
    try:
        selected = finalize_proposals(_root(), run_id=run_id, console=console)
        console.print(f"Finalized proposals for run {selected}")
    except Exception as exc:
        _fail(exc)


@finalize_app.command("portfolio")
def finalize_portfolio_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest run")] = None,
) -> None:
    try:
        selected = finalize_portfolio(_root(), run_id=run_id, console=console)
        console.print(f"Finalized portfolio for run {selected}")
    except Exception as exc:
        _fail(exc)


@app.command("track")
def track_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id to track")] = None,
    live: Annotated[bool, typer.Option("--live", help="Track committed live portfolio")] = False,
) -> None:
    try:
        if run_id and live:
            raise ConfigError("use either --run or --live, not both")
        if live:
            track_live(_root(), console=console)
        elif run_id:
            track_run(_root(), run_id=run_id, console=console)
        else:
            track_all(_root(), console=console)
    except Exception as exc:
        _fail(exc)


@app.command("commit")
def commit_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Run id; defaults to latest finalized portfolio")] = None,
) -> None:
    try:
        commit_run(_root(), run_id=run_id, console=console)
    except Exception as exc:
        _fail(exc)


@app.command("config")
def config_cmd() -> None:
    try:
        root = _root()
        config = load_global_config(root)
        table = Table(title="stockrank config")
        table.add_column("key")
        table.add_column("value")
        table.add_row("api.provider", config.api_provider)
        table.add_row("defaults.strategy", config.default_strategy)
        table.add_row("tracking.benchmark", config.benchmark)
        table.add_row("data.price_history_years", str(config.data.price_history_years))
        table.add_row("data.news_items", str(config.data.news_items))
        table.add_row("data.financial_periods", str(config.data.financial_periods))
        console.print(table)
        for folder in ("strategies", "universes", "modes", "harnesses"):
            names = available_names(root, folder)
            console.print(f"{folder}: {', '.join(names) if names else '(none)'}")
        if config.default_strategy in available_names(root, "strategies"):
            resolved = resolve_strategy(root, config.default_strategy)
            console.print(f"default planned score evaluations: {resolved.planned_score_evaluations}")
    except Exception as exc:
        _fail(exc)


@app.command("version")
def version_cmd() -> None:
    console.print(__version__)


@app.command("help")
def help_cmd(ctx: typer.Context) -> None:
    console.print(ctx.parent.get_help() if ctx.parent else ctx.get_help())


if __name__ == "__main__":
    app()
