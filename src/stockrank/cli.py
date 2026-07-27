from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    ConfigError,
    ModelConfig,
    available_names,
    initialize_project,
    is_initialized,
    load_app_config,
    load_mode,
    model_config,
    read_toml,
    set_default_model,
)
from .credentials import CredentialStore
from .model_catalog import ModelsDevClient
from .setup_wizard import (
    TerminalSetupPrompts,
    configure_setup,
    save_model_profile_with_key,
)

console = Console()
app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Rank stock universes with fresh evidence."
)
runs_app = typer.Typer(no_args_is_help=True, help="Inspect durable ranking runs.")
universe_app = typer.Typer(no_args_is_help=True, help="Inspect and resolve universe definitions.")
mode_app = typer.Typer(no_args_is_help=True, help="Inspect and validate ranking modes.")
model_app = typer.Typer(no_args_is_help=True, help="Inspect and test model profiles.")
massive_app = typer.Typer(no_args_is_help=True, help="Manage the Massive API credential.")
app.add_typer(runs_app, name="runs")
app.add_typer(universe_app, name="universe")
app.add_typer(mode_app, name="mode")
app.add_typer(model_app, name="model")
app.add_typer(massive_app, name="massive")


def _root() -> Path:
    return Path.cwd()


def _fail(exc: Exception) -> None:
    console.print(f"[red]error:[/] {exc}")
    raise typer.Exit(1)


def _credentials() -> CredentialStore:
    return CredentialStore()


def _require_initialized() -> Path:
    root = _root()
    if not is_initialized(root):
        raise ConfigError("project is not initialized; run 'stockrank init'")
    return root


def _prompt_secret(label: str) -> str:
    return typer.prompt(
        label,
        hide_input=True,
        confirmation_prompt=True,
    )


def _run_setup(root: Path) -> None:
    configure_setup(
        root,
        credentials=_credentials(),
        prompts=TerminalSetupPrompts(console),
        catalog=ModelsDevClient(),
    )


@app.command("init")
def init_cmd() -> None:
    try:
        root = _root()
        result = initialize_project(root)
        if result.already_initialized:
            console.print(f"Already initialized in {root}.", soft_wrap=True)
        else:
            console.print(f"Initialized stockrank project in {root}.", soft_wrap=True)
            created = ", ".join(str(path.relative_to(root)) for path in result.created)
            console.print(f"Created {created}.")
            if result.preserved:
                preserved = ", ".join(str(path.relative_to(root)) for path in result.preserved)
                console.print(f"Preserved {preserved}.")
        _run_setup(root)
        console.print("Setup complete.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


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
        root = _require_initialized()
        if resume and any(value is not None for value in (universe, mode, model, profile)):
            raise ConfigError("--resume cannot be combined with new-run selection options")
        if not resume and not universe:
            raise ConfigError("--universe is required for a new run")
        from .pipeline import execute_rank

        run_id = execute_rank(
            root,
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

        resolved = resolve_universe(_require_initialized(), name, console=console)
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
        root = _root()
        if not is_initialized(root):
            console.print("Not initialized. Run 'stockrank init'.")
            return
        config = load_app_config(root)
        if not config.models:
            console.print("(none)")
            return
        for name in sorted(config.models):
            suffix = " (default)" if name == config.defaults.model else ""
            console.print(f"{name}{suffix}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("show")
def model_show_cmd(name: Annotated[str, typer.Argument(help="Model profile name.")]) -> None:
    try:
        profile = model_config(_require_initialized(), name)
        console.print_json(
            json.dumps(
                {
                    "name": profile.name,
                    "provider": profile.provider,
                    "base_url": profile.base_url,
                    "model": profile.model,
                    "api_key_storage": "system keyring",
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


@model_app.command("add")
def model_add_cmd(
    name: Annotated[str, typer.Argument(help="Unique model profile name.")],
    provider: Annotated[
        str,
        typer.Option("--provider", help="Provider identifier."),
    ],
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="OpenAI-compatible API base URL."),
    ],
    model: Annotated[
        str,
        typer.Option("--model", help="Provider model identifier."),
    ],
    reasoning_effort: Annotated[
        str | None,
        typer.Option("--reasoning-effort", help="Optional OpenAI-compatible reasoning effort."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout", min=0.1, help="Request timeout in seconds."),
    ] = 120.0,
    max_retries: Annotated[
        int,
        typer.Option("--max-retries", min=0, help="Provider retry count."),
    ] = 2,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", min=1, help="Maximum parallel model calls."),
    ] = 4,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", min=1, help="Maximum response tokens."),
    ] = 4096,
    make_default: Annotated[
        bool,
        typer.Option("--default", help="Make this the default model profile."),
    ] = False,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace an existing profile with this name."),
    ] = False,
) -> None:
    try:
        root = _require_initialized()
        profile = ModelConfig(
            name=name,
            provider=provider,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            concurrency=concurrency,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        credentials = _credentials()
        made_default = save_model_profile_with_key(
            root,
            profile,
            _prompt_secret(f"API key for {name}"),
            credentials,
            make_default=make_default,
            replace_existing=replace,
        )
        suffix = " and made default" if made_default else ""
        console.print(f"Saved model profile {name!r}{suffix}.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("set-key")
def model_set_key_cmd(
    name: Annotated[str, typer.Argument(help="Existing model profile name.")],
) -> None:
    try:
        model_config(_require_initialized(), name)
        _credentials().set_llm_key(name, _prompt_secret(f"API key for {name}"))
        console.print(f"Stored API key for model profile {name!r}.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("status")
def model_status_cmd(
    name: Annotated[
        str | None,
        typer.Argument(help="Model profile name; defaults from config."),
    ] = None,
) -> None:
    try:
        profile = model_config(_require_initialized(), name)
        state = "stored" if _credentials().get_llm_key(profile.name) else "missing"
        console.print(f"{profile.name}: API key {state}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("default")
def model_default_cmd(
    name: Annotated[str, typer.Argument(help="Existing model profile name.")],
) -> None:
    try:
        _require_initialized()
        set_default_model(name)
        console.print(f"Default model profile set to {name!r}.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@model_app.command("test")
def model_test_cmd(
    name: Annotated[str | None, typer.Argument(help="Model profile name; defaults from config.")] = None,
) -> None:
    try:
        from .pipeline import test_model

        selected = test_model(_require_initialized(), name)
        console.print(f"{selected}: connected")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@massive_app.command("set-key")
def massive_set_key_cmd() -> None:
    try:
        _credentials().set_massive_key(_prompt_secret("Massive API key"))
        console.print("Stored Massive API key.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@massive_app.command("status")
def massive_status_cmd() -> None:
    try:
        state = "stored" if _credentials().get_massive_key() else "missing"
        console.print(f"Massive API key: {state}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@massive_app.command("clear-key")
def massive_clear_key_cmd() -> None:
    try:
        removed = _credentials().delete_massive_key()
        console.print("Removed Massive API key." if removed else "Massive API key was not stored.")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@app.command("track")
def track_cmd(
    run_id: Annotated[str | None, typer.Option("--run", help="Track one finalized run.")] = None,
    all_runs: Annotated[bool, typer.Option("--all", help="Track every finalized run.")] = False,
) -> None:
    try:
        root = _require_initialized()
        if run_id and all_runs:
            raise ConfigError("use either --run or --all")
        from .pipeline import track_rankings

        tracked = track_rankings(root, run_id=run_id, all_runs=all_runs or run_id is None, console=console)
        console.print(f"Tracked {len(tracked)} run(s): {', '.join(tracked)}")
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders domain errors
        _fail(exc)


@app.command("version")
def version_cmd() -> None:
    console.print(__version__)


if __name__ == "__main__":
    app()
