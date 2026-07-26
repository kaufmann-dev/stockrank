from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ConfigError, available_names, load_app_config, load_mode, model_config, read_toml

console = Console()
app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Rank stock universes with fresh evidence."
)
runs_app = typer.Typer(no_args_is_help=True, help="Inspect durable ranking runs.")
universe_app = typer.Typer(no_args_is_help=True, help="Inspect and resolve universe definitions.")
mode_app = typer.Typer(no_args_is_help=True, help="Inspect and validate ranking modes.")
model_app = typer.Typer(no_args_is_help=True, help="Inspect and test model profiles.")
app.add_typer(runs_app, name="runs")
app.add_typer(universe_app, name="universe")
app.add_typer(mode_app, name="mode")
app.add_typer(model_app, name="model")


def _root() -> Path:
    return Path.cwd()


def _fail(exc: Exception) -> None:
    console.print(f"[red]error:[/] {exc}")
    raise typer.Exit(1)


@app.command("rank")
def rank_cmd(
    universe: Annotated[
        str | None,
        typer.Option("--universe", help="Universe name from universes/*.toml; required for a new run."),
    ] = None,
    mode: Annotated[str | None, typer.Option("--mode", help="Saved mode name; defaults from config.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model profile; defaults from config.")] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Race budget profile: low, medium, or high."),
    ] = None,
    resume: Annotated[str | None, typer.Option("--resume", help="Resume an existing run id.")] = None,
) -> None:
    try:
        if resume and any(value is not None for value in (universe, mode, model, profile)):
            raise ConfigError("--resume cannot be combined with new-run selection options")
        if not resume and not universe:
            raise ConfigError("--universe is required for a new run")
        from .pipeline import execute_rank

        run_id = execute_rank(
            _root(),
            universe_name=universe,
            mode_name=mode,
            model_name=model,
            profile_name=profile,
            resume_id=resume,
            console=console,
        )
        console.print(f"Run {run_id} finalized under runs/{run_id}/")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@runs_app.command("list")
def runs_list_cmd() -> None:
    try:
        from .runs import RunStore

        store = RunStore(_root())
        table = Table(title="Ranking runs")
        table.add_column("id")
        table.add_column("state")
        table.add_column("universe")
        table.add_column("mode")
        table.add_column("model")
        for manifest in store.list():
            table.add_row(
                manifest.id,
                manifest.status,
                str(manifest.universe.get("name", "")),
                str(manifest.mode.get("name", "")),
                str(manifest.model.get("name", "")),
            )
        console.print(table)
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@runs_app.command("show")
def runs_show_cmd(run_id: Annotated[str, typer.Argument(help="Run id.")]) -> None:
    try:
        from .runs import RunStore

        manifest = RunStore(_root()).load(run_id)
        console.print_json(json.dumps(manifest.to_dict(), sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@universe_app.command("list")
def universe_list_cmd() -> None:
    names = available_names(_root(), "universes")
    console.print("\n".join(names) if names else "(none)")


@universe_app.command("show")
def universe_show_cmd(name: Annotated[str, typer.Argument(help="Universe name.")]) -> None:
    try:
        console.print_json(json.dumps(read_toml(_root() / "universes" / f"{name}.toml"), sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@universe_app.command("resolve")
def universe_resolve_cmd(name: Annotated[str, typer.Argument(help="Universe name.")]) -> None:
    try:
        from .pipeline import resolve_universe

        resolved = resolve_universe(_root(), name, console=console)
        console.print_json(json.dumps(resolved, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@mode_app.command("list")
def mode_list_cmd() -> None:
    names = available_names(_root(), "modes")
    console.print("\n".join(names) if names else "(none)")


@mode_app.command("show")
def mode_show_cmd(name: Annotated[str, typer.Argument(help="Mode name.")]) -> None:
    try:
        mode = load_mode(_root(), name)
        console.print(f"[bold]{mode.name}[/]\nRank 1: {mode.rank_1_meaning}\n\n{mode.prompt}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@mode_app.command("validate")
def mode_validate_cmd(name: Annotated[str, typer.Argument(help="Mode name.")]) -> None:
    try:
        load_mode(_root(), name)
        console.print(f"{name}: valid")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("list")
def model_list_cmd() -> None:
    try:
        config = load_app_config(_root())
        for name in sorted(config.models):
            suffix = " (default)" if name == config.defaults.model else ""
            console.print(f"{name}{suffix}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("show")
def model_show_cmd(name: Annotated[str, typer.Argument(help="Model profile name.")]) -> None:
    try:
        profile = model_config(_root(), name)
        console.print_json(
            json.dumps(
                {
                    "name": profile.name,
                    "base_url": profile.base_url,
                    "model": profile.model,
                    "api_key_env": profile.api_key_env,
                    "timeout_seconds": profile.timeout_seconds,
                    "max_retries": profile.max_retries,
                    "concurrency": profile.concurrency,
                    "max_tokens": profile.max_tokens,
                    "reasoning_effort": profile.reasoning_effort,
                    "extra_body": profile.extra_body,
                },
                sort_keys=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("test")
def model_test_cmd(
    name: Annotated[str | None, typer.Argument(help="Model profile name; defaults from config.")] = None,
) -> None:
    try:
        from .pipeline import test_model

        selected = test_model(_root(), name)
        console.print(f"{selected}: connected")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@app.command("track")
def track_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Track one finalized run.")] = None,
    all_runs: Annotated[bool, typer.Option("--all", help="Track every finalized run.")] = False,
) -> None:
    try:
        if run_id and all_runs:
            raise ConfigError("use either --run or --all")
        from .pipeline import track_rankings

        tracked = track_rankings(_root(), run_id=run_id, all_runs=all_runs or run_id is None, console=console)
        console.print(f"Tracked {len(tracked)} run(s): {', '.join(tracked)}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@app.command("version")
def version_cmd() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
