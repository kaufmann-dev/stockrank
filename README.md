# stockrank

`stockrank` is a Python CLI for managing external-agent stock ranking runs. It prepares self-contained work folders, renders prompts, parses TOML files written by agent harnesses, aggregates score and portfolio outputs, tracks forward performance, and promotes a finalized run into an explicit live portfolio.

The CLI does not invoke LLMs. Agent execution happens outside `stockrank`: prepare a phase, run the matching harness skill in the generated work folder, then finalize the phase.

## Install

Use Python 3.11 or newer.

```sh
python3 -m pip install -e '.[dev]'
```

Set a Massive API key before preparing score runs:

```sh
printf 'MASSIVE_API_KEY=your_key_here\n' > .env
```

Check the installed command and resolved seed configuration:

```sh
stockrank version
stockrank config
```

## Workflow

Create a score run from a strategy:

```sh
stockrank prepare scores --strategy core
```

This creates `runs/<id>/scores/work/<mode>/<harness>/<ticker>/` folders with downloaded Massive data and a rendered `task.txt` per ticker. Open the intended harness in a `runs/<id>/scores/work/<mode>/<harness>/` folder and invoke `$stockrank-scores`. The harness writes one `results.toml` per ticker folder.

Finalize scores:

```sh
stockrank finalize scores --run <id>
```

Prepare proposal work after scores are finalized:

```sh
stockrank prepare proposals --run <id>
```

Run the matching harness in each `runs/<id>/proposals/work/<mode>__<harness>/` folder using `$stockrank-proposals`. Each folder writes `proposal.toml`.

Finalize proposals and prepare the final portfolio:

```sh
stockrank finalize proposals --run <id>
stockrank prepare portfolio --run <id>
```

Run the portfolio harness in `runs/<id>/portfolio/work/` using `$stockrank-portfolio`. It writes `portfolio.toml`.

Finalize the portfolio:

```sh
stockrank finalize portfolio --run <id>
```

This writes `portfolio.csv`, `trades.csv`, `report.md`, and records the run inception date in `run.toml`.

## Tracking and Live Portfolio

Track a finalized run against the configured benchmark:

```sh
stockrank track --run <id>
```

Track all finalized runs by omitting `--run`, or track committed live state with:

```sh
stockrank track --live
```

Promote a finalized run to the versioned live portfolio:

```sh
stockrank commit --run <id>
```

Commit displays `trades.csv`, asks for confirmation, snapshots the previous `live/portfolio.toml` into `live/history/`, and writes the new live portfolio state.

## Configuration

Global settings live in `stockrank.toml`. Strategies in `strategies/` choose a universe, score modes and harness weights, proposal pairs, and the final portfolio pair. Modes in `modes/` contain phase-specific persona and task text. Harness files in `harnesses/` are labels only; the CLI never launches them. Universes in `universes/` contain runnable ticker lists.

Runtime run data belongs under ignored `runs/`. Live portfolio state under `live/` is versioned and should stay tracked.

## Development

Run tests with:

```sh
python3 -m pytest
```

Tests use temporary projects and fake Massive clients, so they do not require network access. Add tests under `tests/` when changing config validation, prompt rendering, aggregation math, tracking, live commits, or CLI behavior.
