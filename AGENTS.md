# Repository Guidelines

## Project Structure & Module Organization

`src/stockrank/` contains the CLI package. Keep command wiring in `cli.py`, configuration and TOML validation in `config.py`, data access in `massive.py`, phase setup in `prepare.py`, result parsing and aggregation in `aggregate.py` and `finalize.py`, live portfolio promotion in `live.py`, and performance tracking in `tracking.py`.

Static assets are part of the product: prompt templates live in `prompts/`, seed strategies in `strategies/`, modes in `modes/`, harness labels in `harnesses/`, and runnable universes in `universes/`. Execution skills are installed agent skills, not repo-local assets. Runtime output belongs under ignored `runs/`. Versioned live portfolio state belongs under `live/`; do not add it to `.gitignore`.

## Design Invariants

- Mode = content (fused persona + task, never separable); harness = executor label. The CLI never invokes harnesses or LLMs. Which modes/harnesses participate in which phase is decided only in the strategy file, and all weights live only there, normalized at finalize time over available results.
- `prepare scores` snapshots the resolved strategy and every referenced mode/harness definition verbatim into `run.toml`, so editing or deleting source TOML files never changes an existing run.
- Prompt templates in `prompts/` hold fixed mechanics only (files to read, file to write, exact output schema); persona/task come from the mode. Rendered `task.txt` files address paths via `{workdir}` relative to the harness session folder because subagents inherit the session cwd.
- Execution skills contain only batch orchestration and verification — never scoring or construction instructions, which live exclusively in rendered `task.txt` files.
- `prepare` refuses to overwrite an existing phase `work/` without `--force`; `finalize` is idempotent and never aborts on invalid results (warn, exclude, report). `work/` folders are permanent and never cleaned up automatically.
- Tracking is deterministic buy-and-hold math (fixed inception weights, price returns only); the live portfolio changes only through the human-gated `stockrank commit`.

## Build, Test, and Development Commands

```sh
python3 -m pip install -e '.[dev]'
```

Installs the CLI and test dependencies in editable mode.

```sh
python3 -m pytest
```

Runs the full test suite configured in `pyproject.toml`.

```sh
stockrank config
stockrank version
```

Smoke-checks the installed console script and resolved seed configuration.

## Coding Style & Naming Conventions

Use Python 3.11+ features and keep files ASCII unless existing content requires otherwise. Follow the current style: four-space indentation, `from __future__ import annotations`, type hints on public helpers, dataclasses for structured config objects, and `Path` for filesystem work. Keep Typer command functions thin; put behavior in importable functions so tests can call them directly. Use TOML filenames as stable identifiers, for example `modes/mean-reversion.toml` maps to mode name `mean-reversion`.

## Testing Guidelines

Tests use `pytest` and live in `tests/` with names like `test_config.py` and `test_workflow.py`. Prefer temporary project fixtures and fake Massive clients over network calls. Add focused tests for config validation, prompt rendering, aggregation math, live commit behavior, tracking math, and CLI happy paths when touching those areas.

## Commit & Pull Request Guidelines

Git history uses Conventional Commits, such as `feat(cli): add stockrank workflow` and `docs(plan): add comprehensive plan for stockrank CLI`. Keep subjects lowercase, present tense, and scoped when useful.

Pull requests should describe the workflow impact, list verification commands run, and call out any changed TOML schemas, prompt templates, or generated output formats.

## Security & Configuration Tips

Read Massive credentials only from `.env` via `MASSIVE_API_KEY`; never commit secrets. Do not invoke LLMs from the CLI. Generated harness outputs stay in `runs/`, while committed portfolio provenance stays in `live/`.
