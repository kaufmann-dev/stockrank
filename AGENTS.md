# Repository Guidelines

## Project Structure

`src/stockrank/` is the CLI package. Keep Typer command functions in `cli.py` thin; put behavior in importable functions so tests can call them directly.

Static assets are part of the product: `prompts/`, `strategies/`, `modes/`, `harnesses/`, `universes/`. Execution skills are installed agent skills, not repo-local assets. Runtime output belongs under ignored `runs/`; live portfolio state under `live/` stays versioned — never add `live/` to `.gitignore`.

## Design Invariants

- Never invoke LLMs or agent harnesses from the CLI. Harness files are pure labels.
- Keep prompt templates in `prompts/` limited to fixed mechanics (files to read, file to write, exact output schema); persona and task always come from the mode.
- Address paths in rendered `task.txt` files via the `{workdir}` placeholder relative to the harness session folder — subagents inherit the session cwd, so bare relative paths would collide across tickers.
- Use TOML filenames as stable identifiers: `modes/mean-reversion.toml` maps to mode name `mean-reversion`.

## Build, Test, and Development Commands

```sh
python3 -m pip install -e '.[dev]'
```

```sh
python3 -m pytest
```

```sh
stockrank config
stockrank version
```

## Testing Guidelines

Tests live in `tests/` with names like `test_config.py`. Always use temporary project fixtures and fake Massive clients instead of network calls.

## Commit & Pull Request Guidelines

Use Conventional Commits with lowercase, present-tense subjects, scoped when useful. In PRs, describe the workflow impact, list verification commands run, and call out any changed TOML schemas, prompt templates, or generated output formats.

## Security

Read Massive credentials only from `.env` via `MASSIVE_API_KEY`; never commit secrets.
