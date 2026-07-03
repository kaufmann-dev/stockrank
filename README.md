# stockrank

`stockrank` is a Python CLI for managing external-agent stock ranking runs. It prepares self-contained work folders, renders prompts, parses TOML files written by agent harnesses, aggregates score and portfolio outputs, tracks forward performance, and promotes a finalized run into an explicit live portfolio.

The CLI does not invoke LLMs. Agent execution happens outside `stockrank`: prepare a phase, run the matching harness skill in the generated work folder, then finalize the phase.

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

`prepare scores` creates a new run (run id = unix timestamp) and writes `run.toml`, a manifest containing a verbatim snapshot of the resolved strategy plus the persona/task text of every referenced mode and harness — later edits to the source TOML files never affect an existing run. `proposals` and `portfolio` operate on an existing run (default: latest in `runs/`, override with `--run <id>`).

`prepare` refuses to overwrite an existing `work/` for a phase unless `--force`. `finalize` is idempotent and can be re-run any time, for example after more harnesses finished. `work/` folders are permanent parts of a run and are never cleaned up automatically.

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

This creates `runs/<id>/scores/work/<mode>/<harness>/<ticker>/` folders with downloaded Massive data and a rendered `task.txt` per ticker. Each ticker's `data/` folder contains `details.json`, `prices_daily.csv`, `financials.json`, `dividends.json`, `splits.json`, and `news.json`; an endpoint outside the API plan produces an error stub instead, and prompts tell harnesses to work with whatever data is present.

Open the intended harness in a `runs/<id>/scores/work/<mode>/<harness>/` folder and invoke `$stockrank-scores`. Harness identity is carried entirely by the folder you open the session in — you are the router — so multiple sessions, one per harness, can run in parallel against disjoint folders. The harness writes one `results.toml` per ticker folder:

```toml
ticker = "AAPL"
score = 78            # integer 0-100
confidence = 0.8      # float 0-1
summary = "1-3 sentence rationale."
```

Finalize scores:

```sh
stockrank finalize scores --run <id>
```

Aggregation: per ticker, each mode score is the harness-weighted average over harnesses that produced a valid `results.toml`, and `final_score` is the mode-weighted average over modes with at least one valid result; weights are re-normalized over what is available. A ticker with no valid result anywhere gets `NaN` and is listed as missing in `report.md`. Invalid results are warned about and excluded, never fatal. Outputs: `scores.csv` (one row per ticker with raw, per-mode, and final scores plus rank), `rationales.csv` (one row per evaluation: ticker, mode, harness, score, confidence, summary — the qualitative record downstream builders read), and `report.md` (human digest).

Prepare proposal work after scores are finalized:

```sh
stockrank prepare proposals --run <id>
```

Run the matching harness in each `runs/<id>/proposals/work/<mode>__<harness>/` folder using `$stockrank-proposals`. Each folder contains copies of the finalized `scores.csv`, `rationales.csv`, and `report.md` plus a rendered `task.txt`, and the harness writes `proposal.toml`:

```toml
mode = "balanced-builder"
harness = "claude-code"
summary = "Overall portfolio thesis."

[[positions]]
ticker = "AAPL"
weight = 0.08         # all weights must sum to 1.0
rationale = "..."
```

Finalize proposals and prepare the final portfolio:

```sh
stockrank finalize proposals --run <id>
stockrank prepare portfolio --run <id>
```

Run the portfolio harness in `runs/<id>/portfolio/work/` using `$stockrank-portfolio`. The work folder contains copies of all upstream outputs plus `current.csv` and `current.md` — the live portfolio and its provenance. The prompt instructs the allocator to treat the current book as the starting point and justify every deviation; if the book is empty it builds from scratch, and since the book may have been built under a different strategy, holdovers are judged by weighing their original rationale against the current lens, not by today's scores alone. The harness writes `portfolio.toml` (same schema as `proposal.toml`, without the `mode`/`harness` fields).

Finalize the portfolio:

```sh
stockrank finalize portfolio --run <id>
```

This writes `portfolio.csv`, `trades.csv` (deterministic diff vs. the live portfolio: ticker, current weight, target weight, delta), `report.md` (thesis, positions, comparison against each proposal, and implied turnover), and records the run inception date in `run.toml`. Finalize validation across phases: proposal/portfolio weights must sum to 1.0 ± 0.01 (warned and renormalized otherwise), tickers must belong to the run's universe (warned and dropped otherwise), and `mode`/`harness` fields must match the folder they sit in.

## Tracking and Live Portfolio

Track a finalized run against the configured benchmark:

```sh
stockrank track --run <id>
```

Track all finalized runs by omitting `--run`, or track committed live state with:

```sh
stockrank track --live
```

Tracking is purely deterministic paper-tracking, no agents involved. It tracks the final portfolio and every proposal of a run against the benchmark from a common inception (the first trading day at or after `finalize portfolio`), normalized to 1.0 at the inception close. The math is buy-and-hold with fixed inception weights and price returns only — no rebalancing, no dividends, no transaction costs. Missing prices (e.g. delisted tickers) carry the last available close forward with a warning. Results go to `runs/<id>/portfolio/performance.csv`; `--live` chains one segment per commit period multiplicatively and writes `live/performance.csv`. It is idempotent and re-runnable anytime — there is no scheduling or daemon.

Promote a finalized run to the versioned live portfolio:

```sh
stockrank commit --run <id>
```

Commit displays `trades.csv`, asks for confirmation, snapshots the previous `live/portfolio.toml` into `live/history/`, and writes the new live portfolio state. A run's finalized portfolio is only a proposal for the new book; the live portfolio changes exclusively through this explicit, human-gated commit. `live/portfolio.toml` carries full provenance from the committed run — run id, timestamp, strategy name, mode/weight summary, thesis, and per-position rationales — which the next run's `prepare portfolio` renders into `current.csv`/`current.md`. Absent or empty means 100% cash. Executing `trades.csv` at a broker remains your manual job.

## Configuration

Global settings live in `stockrank.toml`: the API provider (`[api]`), per-ticker download depth (`[data]` — `price_history_years`, `news_items`, `financial_periods`), the default strategy (`[defaults]`), and the tracking benchmark (`[tracking]`).

Strategies in `strategies/` choose a universe, score modes and harness weights, proposal pairs, a `num_positions` hint injected into the proposals prompt, and the final portfolio pair:

```toml
name = "core"
universe = "sp100"

[[scores.modes]]
name = "momentum"
weight = 1.0
harnesses = [
  { name = "claude-code", weight = 1.0 },
  { name = "codex",       weight = 0.5 },
]

[proposals]
num_positions = 15
runs = [
  { mode = "balanced-builder", harness = "claude-code" },
]

[portfolio]
mode = "final-allocator"
harness = "claude-code"
```

All weights live only in the strategy file and are normalized over the selected modes/harnesses at finalize time; the same harness can carry different weights under different modes or strategies. Modes in `modes/` contain phase-specific persona and task text. Harness files in `harnesses/` are labels only; the CLI never launches them. Universes in `universes/` contain runnable ticker lists. Modes and harnesses are global pools — which of them participate in which phase is decided only in the strategy file.

Prompt templates in `prompts/` contain only the fixed per-phase mechanics (what to read, what to write, the exact output schema); `persona` and `task` are injected from the mode. Every rendered `task.txt` states its target folder explicitly via a `{workdir}` placeholder relative to the harness session's folder, because subagents inherit the session's working directory — for scores that is the ticker folder name, for proposals and portfolio it is `.`.

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

Tests use temporary projects and fake Massive clients, so they do not require network access. Add tests under `tests/` when changing config validation, prompt rendering, aggregation math, tracking, live commits, or CLI behavior.
