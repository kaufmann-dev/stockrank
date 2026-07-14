# stockrank

`stockrank` is a Python CLI for managing external-agent stock ranking runs. It prepares self-contained work folders, renders prompts, parses TOML files written by agent harnesses, aggregates score and portfolio outputs, tracks forward performance, and promotes a finalized run into an explicit live portfolio.

The CLI does not invoke LLMs. Agent execution happens outside `stockrank`: prepare a phase, run the matching project-scoped skill from `.agents/skills/` in the generated work folder, then finalize the phase.

## Concepts

- A **mode** is the complete prompt content of an evaluation: a fused `persona` ("You are…") and `task` (what to evaluate, how to score, or how to build a portfolio). Persona and task are deliberately not separable — the voice belongs to the lens. A mode may declare an optional `phase` (`scores`, `proposals`, or `portfolio`); if present, `prepare` fails fast when a strategy references the mode in a different phase.
- A **harness** is the execution environment that runs a mode, for example Claude Code on one LLM and Codex on another. Harnesses are pure labels to the CLI — it never invokes them — but they are the identity that folder names and weights attach to. Running the same mode across multiple harnesses is how you get multi-model diversity; running several modes is how you get stylistic diversity.
- A **strategy** defines the complete characteristics of a run in one file: universe, score modes with weights, harnesses per mode with weights, proposal (mode, harness) pairs, and the final portfolio pair. There are no per-run overrides via flags — a different setup means another strategy TOML.

Every run moves through three phases, and every phase has the same three steps: `prepare` (CLI builds self-contained work folders with data and `task.txt` prompts), execute (you open a harness in the work folder and invoke the matching skill), and `finalize` (CLI parses result TOMLs and writes `.csv`/`.md` outputs).

| Phase       | Unit of work                                                              | Harness output per unit          |
| ----------- | ------------------------------------------------------------------------- | -------------------------------- |
| `scores`    | One evaluation per (mode × harness × ticker), score 0–100                 | `results.toml` per ticker folder |
| `proposals` | One portfolio per (mode × harness) pair, built from aggregated scores     | `proposal.toml` per pair folder  |
| `portfolio` | One single (mode × harness) merges all proposals into the final portfolio | `portfolio.toml`                 |

`prepare scores` creates a new run and snapshots the resolved strategy plus all referenced mode/harness definitions into `run.toml`, so later edits to the source TOML files never affect an existing run. `proposals` and `portfolio` operate on an existing run (default: latest in `runs/`, override with `--run <id>`). `finalize` is idempotent and can be re-run any time, for example after more harnesses finished.

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

This creates `runs/<id>/scores/work/<mode>/<harness>/<ticker>/` folders, each with downloaded Massive data (details, daily prices, financials, corporate actions, news) and a rendered `task.txt`.

Open the intended harness in a `runs/<id>/scores/work/<mode>/<harness>/` folder and invoke `$stockrank-scores`. Harness identity is carried entirely by the folder you open the session in — you are the router — so multiple sessions, one per harness, can run in parallel against disjoint folders. The harness writes one `results.toml` per ticker folder.

Finalize scores:

```sh
stockrank finalize scores --run <id>
```

Scores aggregate as weighted averages — per harness within a mode, then per mode into `final_score` — with weights re-normalized over the results actually available; invalid or missing results are warned about and excluded, never fatal. Outputs: `scores.csv` (ranked numbers), `rationales.csv` (per-evaluation reasoning, read by downstream builders), and `report.md` (human digest).

Prepare proposal work after scores are finalized:

```sh
stockrank prepare proposals --run <id>
```

Run the matching harness in each `runs/<id>/proposals/work/<mode>__<harness>/` folder using `$stockrank-proposals`. Each folder contains copies of the finalized scores outputs plus a rendered `task.txt`, and the harness writes `proposal.toml`.

Finalize proposals and prepare the final portfolio:

```sh
stockrank finalize proposals --run <id>
stockrank prepare portfolio --run <id>
```

Run the portfolio harness in `runs/<id>/portfolio/work/` using `$stockrank-portfolio`. The work folder contains copies of all upstream outputs plus `current.csv` and `current.md` — the live portfolio and its provenance. The allocator treats the current book as the starting point and justifies every deviation; if the book is empty it builds from scratch. The harness writes `portfolio.toml`.

Finalize the portfolio:

```sh
stockrank finalize portfolio --run <id>
```

This writes `portfolio.csv`, `trades.csv` (the diff vs. the live portfolio), `report.md`, and records the run inception date in `run.toml`.

## Tracking and Live Portfolio

Track a finalized run against the configured benchmark:

```sh
stockrank track --run <id>
```

Track all finalized runs by omitting `--run`, or track committed live state with:

```sh
stockrank track --live
```

Tracking is deterministic paper-tracking, no agents involved: buy-and-hold with fixed inception weights and price returns only — no rebalancing, no dividends, no transaction costs. It covers the final portfolio and every proposal of a run, so builder modes and harnesses become empirically comparable over time. Results go to `runs/<id>/portfolio/performance.csv` and `live/performance.csv`; it is idempotent and re-runnable anytime.

Promote a finalized run to the versioned live portfolio:

```sh
stockrank commit --run <id>
```

A run's finalized portfolio is only a proposal for the new book; the live portfolio changes exclusively through this explicit, human-gated commit, which snapshots the previous state into `live/history/`. The committed book, with its provenance, is what the next run's portfolio phase manages *from*. An absent or empty live portfolio means 100% cash. Executing `trades.csv` at a broker remains your manual job.

## Configuration

Global settings live in `stockrank.toml`: API provider, per-ticker download depth, default strategy, and tracking benchmark. Strategies live in `strategies/` (see `strategies/core.toml` for the schema), modes in `modes/`, harness labels in `harnesses/`, and universes in `universes/`. All weights live only in the strategy file and are normalized over the selected modes/harnesses at finalize time; modes and harnesses are global pools, and which of them participate in which phase is decided only in the strategy file.

Runtime run data belongs under ignored `runs/`. Live portfolio state under `live/` is versioned and should stay tracked.

## Non-Goals

- The CLI never invokes any LLM or agent harness.
- No backtesting (historical simulation), no broker integration, no scheduling. Forward tracking is the only performance feature.
- No per-run weight or selection overrides via flags — a run is fully defined by its strategy file.

## Development

Run tests with:

```sh
python3 -m pytest
```

Tests use temporary projects and fake Massive clients, so they do not require network access.
