# stockrank — Final Plan

## 1. Concept

`stockrank` is a minimal Python CLI that orchestrates LLM-based stock ranking. It never calls LLMs itself. Every run moves through three phases, and every phase has the same three steps:

1. **prepare** — the CLI builds a self-contained working directory (data + `task.txt` prompts).
2. **execute** — the *user* cds into a prepared work folder, opens an external agent harness there (Claude Code, Codex, etc.), and invokes the matching execution skill (§5), which takes no arguments. The harness writes results as TOML files into the prepared folders. This step is entirely outside the CLI's scope.
3. **finalize** — the CLI parses the result TOML files, aggregates, and writes `.csv` and `.md` outputs.

Two core concepts:

- A **mode** is the complete prompt content of an evaluation: a fused `persona` ("You are…") and `task` (what to evaluate / how to score / how to build a portfolio). Persona and task are deliberately *not* separable — the voice belongs to the lens.
- A **harness** is the execution environment that runs a mode: e.g. Claude Code on one LLM, Codex on another. Harnesses are pure labels to the CLI (it never invokes them), but they are the identity that folder names and weights attach to. Running the same mode across multiple harnesses is how you get multi-model diversity.

Phases, in order:

| Phase | Unit of work | Harness output per unit |
|---|---|---|
| `scores` | One evaluation per (mode × harness × ticker), score 0–100 | `results.toml` per ticker folder |
| `proposals` | One portfolio per (mode × harness) pair, built from the aggregated scores | `proposal.toml` per pair folder |
| `portfolio` | One single (mode × harness) merges all proposals into the final portfolio | `portfolio.toml` |

`prepare scores` creates a new run. `proposals` and `portfolio` always operate on an existing run (default: the latest run in `runs/`, override with `--run <id>`).

## 2. Project layout (user working directory)

```
.env                      # MASSIVE_API_KEY=...
.gitignore                # ignores runs/, .env, __pycache__, etc.
README.md
pyproject.toml
stockrank.toml            # global config, see §3
src/stockrank/            # package: cli.py, config.py, massive.py, prepare.py, finalize.py, aggregate.py
prompts/
  scores.txt              # phase prompt templates (fixed mechanics), see §5
  proposals.txt
  portfolio.txt
strategies/
  core.toml               # complete run definition, see §3
  test.toml               # minimal Dow 30 test run definition
modes/
  momentum.toml           # scoring modes
  mean-reversion.toml
  best-bet.toml
  confluence.toml
  distress.toml
  mispricing.toml
  moat-durability.toml
  stress.toml
  balanced-builder.toml   # proposal modes
  final-allocator.toml    # portfolio modes
harnesses/
  claude-code.toml
  codex.toml
live/
  portfolio.toml          # the current live portfolio + provenance, see §10
  history/                # snapshot of the previous live portfolio per commit
universes/
  sp100.toml
  dow30.toml
  global-titans-50.toml
runs/
  1783077525/             # run id = unix timestamp at `prepare scores`
    run.toml              # manifest, see §6
    scores/
      work/
        momentum/
          claude-code/    # ← open this harness here
            AAPL/
              data/       # financial data files, see §7
              task.txt    # rendered prompt
              results.toml  # written by the harness
            MSFT/
              ...
          codex/
            ...
        mean-reversion/
          ...
      scores.csv
      rationales.csv    # one row per (ticker × mode × harness): score + reasoning
      report.md
    proposals/
      work/
        balanced-builder__claude-code/   # one folder per (mode × harness) pair
          scores.csv      # ┐ copies of the finalized scores outputs:
          rationales.csv  # │ numbers, per-evaluation reasoning,
          report.md       # ┘ and the human digest with run context
          task.txt
          proposal.toml   # written by the harness
        balanced-builder__codex/
          ...
      proposals.csv
      proposals.md
    portfolio/
      work/
        scores.csv        # copies of upstream outputs
        rationales.csv
        report.md
        proposals.csv
        proposals.md
        current.csv       # live portfolio: ticker, weight, rationale (may be empty), see §10
        current.md        # provenance of the live portfolio: strategy, weights, thesis, see §10
        task.txt
        portfolio.toml    # written by the harness
      portfolio.csv
      trades.csv        # diff vs. current live portfolio, computed by finalize
      report.md
      performance.csv   # written/updated by `stockrank track`, see §9
```

## 3. Config files

### stockrank.toml
```toml
[api]
provider = "massive"          # key read from MASSIVE_API_KEY in .env

[data]                        # what `prepare scores` downloads per ticker
price_history_years = 2
news_items = 10
financial_periods = 8         # last N quarterly statements

[defaults]
strategy = "core"             # strategy used when none is specified

[tracking]
benchmark = "SPY"             # ticker used as benchmark by `stockrank track`
```

### strategies/*.toml
A strategy defines the **complete characteristics of a run** in one file: universe, modes with weights, harnesses per mode with weights, proposal pairs, and the portfolio pair. Modes and harnesses are referenced by filename.
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

[[scores.modes]]
name = "mean-reversion"
weight = 0.5
harnesses = [
  { name = "claude-code", weight = 1.0 },
]

[proposals]
num_positions = 15            # hint (not hard constraint) injected into proposals task.txt
runs = [
  { mode = "balanced-builder", harness = "claude-code" },
  { mode = "balanced-builder", harness = "codex" },
]

[portfolio]
mode = "final-allocator"
harness = "claude-code"
```

### universes/*.toml
```toml
name = "S&P 100"
tickers = ["AAPL", "MSFT", "GOOGL", ...]
```

### modes/*.toml
The full prompt content of one evaluation lens: persona and task, fused. A mode is written *for* a phase (a scoring mode scores; a builder mode constructs portfolios). The optional `phase` field declares which one — if present, `prepare` fails fast when the strategy references the mode in a different phase; if absent, the mode is usable anywhere (no warning).
```toml
name = "momentum"
phase = "scores"              # optional: "scores" | "proposals" | "portfolio"
persona = """You are a battle-hardened technical analyst who trusts price action
over narratives and is skeptical of stale trends."""
task = """Evaluate the stock's price and earnings momentum over the last 12 months.
Consider trend strength, consistency, volume confirmation, and recent earnings surprises.
Assign a score from 0 (worst) to 100 (best)."""
```
A proposal/portfolio mode looks the same, with a construction task instead:
```toml
name = "balanced-builder"
phase = "proposals"
persona = """You are a pragmatic portfolio manager who values diversification
across sectors and hates concentration risk."""
task = """Build a long-only portfolio from the highest-conviction names in the scores.
Balance across sectors; no position above 10%."""
```

### harnesses/*.toml
A pure label for an execution environment. The CLI never invokes it — the file exists so folder names, weights, and reports have a stable identity, and so `description` documents what the user should open there.
```toml
name = "claude-code"
description = "Claude Code CLI running Claude Opus 4.8"
```

**Weighting decision:** all weights live only in the strategy file and are normalized over the selected modes/harnesses at finalize time. The same harness can carry different weights under different modes or strategies. The full strategy is snapshotted into `run.toml` at prepare time so later edits don't change an existing run.

**Separation decision:** *mode = content (who + what), harness = executor (which LLM/tool)*. Persona lives in the mode because it usually cannot be decoupled from the task. Multi-model diversity comes from running one mode across several harnesses; stylistic diversity comes from several modes. Both `modes/` and `harnesses/` are global pools — which of them participate in which phase is decided only in the strategy file.

## 4. CLI surface

```
stockrank prepare  scores      # [--strategy NAME], interactive strategy picker if omitted; creates new run
stockrank prepare  proposals   # [--run ID], requires finalized scores; everything else comes from the run's strategy
stockrank prepare  portfolio   # [--run ID], requires finalized proposals
stockrank finalize scores      # [--run ID], default latest
stockrank finalize proposals   # [--run ID]
stockrank finalize portfolio   # [--run ID]
stockrank track                # [--run ID | --live], default: all runs with a finalized portfolio; see §9
stockrank commit               # [--run ID], promote a run's finalized portfolio to the live portfolio; see §10
stockrank config               # print resolved config + available strategies/universes/modes/harnesses
stockrank help
stockrank version
```

The strategy file replaces all interactive selection: the only question `prepare scores` ever asks is *which strategy* (numbered list, default from `stockrank.toml`), and later prepares ask nothing at all since the run's snapshotted strategy already defines their pairs. Before creating anything, `prepare scores` validates that every referenced universe/mode/harness file exists, that no mode with a declared `phase` is referenced in a different phase, that `[proposals].runs` contains no duplicate (mode, harness) pair (folder names would collide), and echoes the total planned evaluations (sum over modes of |harnesses for that mode| × |tickers|).

`prepare` refuses to overwrite an existing `work/` for that phase unless `--force`. `finalize` is idempotent and can be re-run any time (e.g. after more harnesses finished). `work/` folders are permanent parts of a run, never cleaned up automatically: they hold the raw harness outputs that `finalize` parses and the exact prompts/data each evaluation was based on.

## 5. Prompt composition

Each `task.txt` is rendered from the phase template in `prompts/` with `str.format`-style placeholders. The templates contain only the fixed mechanics (what files to read, what file to write, the exact output schema); `persona` and `task` are injected from the mode.

**Path convention (important):** subagents spawned by a harness usually inherit the *session's* working directory, not the ticker folder — so bare relative paths like `./results.toml` would resolve wrongly and collide across tickers. Therefore every rendered `task.txt` states its target explicitly via a `{workdir}` placeholder, rendered **relative to the folder the harness session runs in** (the folder the user cds into — see execution skills below). For scores, the session runs in the mode/harness folder, so `{workdir}` is simply the ticker folder name: "your working folder is `AAPL`; read `AAPL/data/`, write `AAPL/results.toml`" — unambiguous per subagent even though all subagents share the session's cwd. For proposals and portfolio, the session runs in the work folder itself, so `{workdir}` renders as `.`.

- **scores.txt** placeholders: `{persona}` `{task}` `{ticker}` `{mode}` `{universe}` `{workdir}`. Instructs the harness to read everything in `{workdir}/data/`, perform the evaluation described by `{task}` in the voice/judgment of `{persona}`, and write `{workdir}/results.toml` in exactly this schema:
  ```toml
  ticker = "AAPL"
  score = 78            # integer 0–100
  confidence = 0.8      # float 0–1
  summary = "1–3 sentence rationale."
  ```
- **proposals.txt** placeholders: `{persona}` `{task}` `{universe}` `{num_positions_hint}` `{workdir}`. Instructs the harness to read `{workdir}/scores.csv` (the numbers), `{workdir}/rationales.csv` (the per-evaluation reasoning — why each stock scored the way it did, per mode and harness), and `{workdir}/report.md` (run context: strategy, mode weights) and write `{workdir}/proposal.toml`:
  ```toml
  mode = "balanced-builder"
  harness = "claude-code"
  summary = "Overall portfolio thesis."
  [[positions]]
  ticker = "AAPL"
  weight = 0.08         # all weights must sum to 1.0
  rationale = "..."
  ```
- **portfolio.txt** placeholders: `{persona}` `{task}` `{workdir}`. Reads `{workdir}/scores.csv`, `{workdir}/rationales.csv`, `{workdir}/report.md`, `{workdir}/proposals.csv`, `{workdir}/proposals.md`, and `{workdir}/current.csv` + `{workdir}/current.md` (the live portfolio — the existing book — with its provenance: which strategy built it, its mode weights, its thesis, and each position's original rationale). Fixed mechanics instruct: treat the current book as the starting point and justify every deviation from it; if it is empty, build from scratch; and since the book may have been constructed under a *different strategy* (regimes change — the previous run may have weighted momentum where this one weights mean-reversion), judge holdovers by weighing their original rationale against the current lens, not by today's scores alone. Turnover discipline (limits, replacement thresholds) is policy and therefore belongs in the portfolio mode's `task`, not in the template. Writes `{workdir}/portfolio.toml` (same schema as `proposal.toml`, without the `mode`/`harness` fields).

### Execution skills

The execute step is driven by three static skills so the user never types an orchestration prompt. The workflow: **cd into the target work folder, open the harness there, invoke the skill — no arguments.** Skills derive everything from the current directory. Multiple sessions — one per harness — can run in parallel, since they write into disjoint folders. Harnesses have no self-awareness of which harness they are: identity is carried entirely by the folder the user runs the session in (the user is the router). **First actions of every skill:** (1) validate that the cwd is the right kind of folder (ticker subfolders with `task.txt` files, or a `task.txt` directly) and abort with a clear message otherwise; (2) parse the designated harness (and mode) from the cwd path and ask the user to confirm that this session *is* that harness before doing anything — this guards against opening the wrong tool in a folder, which would silently mis-attribute results.

The canonical agent skills live outside this repo as installed harness skills. Harnesses invoke them by name from the prepared work folders.

- **stockrank-scores** — run from a `runs/<id>/scores/work/<mode>/<harness>/` folder. Spawns one subagent per ticker subfolder, in parallel batches (5–10 at a time); each subagent does nothing but follow its own folder's `task.txt`. Afterwards verifies that every ticker folder contains a parseable `results.toml`, retries missing/invalid ones once, and reports a completion summary (done / failed tickers).
- **stockrank-proposals** — run from a `runs/<id>/proposals/work/<mode>__<harness>/` folder. Follows its `task.txt`, verifies `proposal.toml` parses and weights sum to ~1.0, reports.
- **stockrank-portfolio** — run from `runs/<id>/portfolio/work/`. Same pattern for `portfolio.toml`.

Skills contain *only* batch orchestration and verification — never scoring or construction instructions, which live exclusively in the rendered `task.txt` files.

## 6. Run manifest (`run.toml`)

Written at `prepare scores`. Lets `finalize` and later prepares know exactly what to expect without guessing from the filesystem. It contains the run metadata plus a **verbatim snapshot of the resolved strategy** (same schema as §3), so deleting or editing `strategies/core.toml` never affects an existing run. Mode and harness definitions referenced by the strategy are snapshotted too (their persona/task text), so a run is fully self-describing.

```toml
id = 1783077525
created = "2026-07-03T10:15:00Z"
tickers = ["AAPL", ...]       # resolved from the universe at prepare time
inception = "2026-07-06"      # added by `finalize portfolio`; used by `stockrank track` (§9)

[strategy]                     # verbatim copy of strategies/core.toml
name = "core"
universe = "sp100"
# ... [[scores.modes]], [proposals], [portfolio] exactly as in §3

[[snapshots.modes]]            # verbatim copies of every referenced mode/harness file
name = "momentum"
persona = "..."
task = "..."

[[snapshots.harnesses]]
name = "claude-code"
description = "..."
```

## 7. Data acquisition (`data/` per ticker)

Source: **Massive API (formerly Polygon.io), Stocks Starter plan** — unlimited REST calls, 15-min delayed data, several years of history. Files written per ticker, in LLM-friendly formats:

| File | Endpoint (Massive REST) | Content |
|---|---|---|
| `details.json` | Ticker Details | name, sector, market cap, description, shares outstanding |
| `prices_daily.csv` | Aggregates (daily bars) | OHLCV for the last `price_history_years` years |
| `financials.json` | Financials (vX) | last `financial_periods` quarterly income/balance/cash-flow statements |
| `dividends.json` + `splits.json` | Dividends / Splits | corporate actions history |
| `news.json` | Ticker News | last `news_items` headlines with dates + summaries |

Implementation notes: single thin client module (`massive.py`, `httpx`), API key from `.env`, modest concurrency (e.g. 8 parallel tickers), retry with backoff on 429/5xx. If an endpoint returns 403 (not in plan), write the file with an `{"error": ...}` stub and continue — the prompt tells harnesses to work with whatever data is present. No cache directory: each ticker is fetched once (in memory) and the resulting files are written directly into every mode/harness ticker folder that needs it.

## 8. Aggregation math (finalize scores)

For each ticker:
- `mode_score = Σ_harnesses (norm_harness_weight × harness_score)` over harnesses that produced a valid `results.toml` for that mode; weights re-normalized over available harnesses.
- `final_score = Σ_modes (norm_mode_weight × mode_score)` over modes with at least one valid result; weights re-normalized likewise.
- A ticker with no valid results anywhere gets `final_score = NaN` and is listed under "missing" in `report.md`.

Invalid/missing `results.toml` (unparseable, score out of range) → warn, exclude, list in report. Never abort.

### Output schemas
- **scores.csv** — one row per ticker; columns: `ticker`, one column per `(mode)__(harness)` raw score, one column per mode aggregate, `final_score`, `rank` (1 = best). Sorted by rank.
- **rationales.csv** — the canonical qualitative record, long format, one row per evaluation: `ticker`, `mode`, `harness`, `score`, `confidence`, `summary`. Complete coverage of every valid `results.toml`; this is the file downstream builders use to understand *why* a stock scored as it did.
- **report.md** — the human digest: run metadata (id, universe, modes×harnesses matrix, weights), top-25 table by final score, per-mode top-10, missing/invalid results section. No rationale appendix — that lives in `rationales.csv`.
- **proposals.csv** — long format: `mode`, `harness`, `ticker`, `weight`, `rationale`. **proposals.md** — per-pair section with thesis + positions table.
- **portfolio.csv** — `ticker`, `weight`, `rationale`. **trades.csv** — deterministic diff vs. the live portfolio at finalize time: `ticker`, `current_weight`, `target_weight`, `delta`. **report.md** — final thesis, positions table, comparison of final weights vs. each proposal, and implied turnover (`0.5 × Σ|delta|`) for review before committing.

Finalize validation: proposal/portfolio weights must sum to 1.0 ± 0.01 (warn and renormalize otherwise); tickers must be members of the run's universe (warn and drop otherwise); `mode`/`harness` fields in `proposal.toml` must match the folder they sit in (warn otherwise).

**Format rationale:** each file has one consumer-defined job. `.csv` = structured data — numbers (`scores.csv`) and structured reasoning (`rationales.csv`, `proposals.csv`) — read by downstream harnesses and tooling. `.md` = human-oriented digests and context, also copied downstream where run context matters.

## 9. Forward testing (`stockrank track`)

Purely deterministic, no agents involved — therefore a plain command, not a phase. It paper-tracks every finalized portfolio *and every proposal* of a run against the benchmark, from a common inception, so that over time the performance of builder modes and harnesses becomes empirically comparable.

- **Inception:** when `finalize portfolio` completes, it records `inception` (the first trading day at/after that timestamp) in `run.toml`. All series of that run — final portfolio, every proposal, benchmark — start at the close of that day, normalized to 1.0.
- **Math:** buy-and-hold with fixed inception weights, price returns only (no rebalancing, no dividends, no transaction costs). `value_t = Σ_positions weight × close_t / close_inception`.
- **Data:** daily closes via the existing Massive client, fetched on demand at track time. A ticker with missing prices (e.g. delisted) carries its last available close forward, with a warning.
- **Output:** writes/overwrites `runs/<id>/portfolio/performance.csv` — columns: `date`, `benchmark`, `portfolio`, one column per `(mode)__(harness)` proposal. Prints a per-run summary and, when run without `--run`, a cross-run comparison table (run id, strategy, inception, days elapsed, portfolio return, benchmark return, excess, best/worst proposal).
- **Operation:** idempotent, re-runnable anytime, no scheduling and no daemon — the user runs it whenever they want an update.
- **`--live`:** tracks the *managed* portfolio across commits (§10): one segment per commit period, each segment holding that period's committed weights, segments chained multiplicatively; the benchmark is chained over the same dates. Writes `live/performance.csv` (`date`, `benchmark`, `live`) and prints the live track record.

## 10. Live portfolio (`stockrank commit`)

A run's finalized portfolio is only a *proposal for the new book*. The live portfolio is separate state in `live/` and changes exclusively through an explicit, human-gated commit:

- **`live/portfolio.toml`** — the current book plus its full provenance, all captured from the committed run: `run_id`, `committed` timestamp, `strategy` name, a compact mode/weight summary (e.g. `"momentum×1.0, mean-reversion×0.5"`), the portfolio `summary` (thesis), and `[[positions]]` (ticker, weight, rationale). Absent or empty = 100% cash; the portfolio phase then builds from scratch.
- **`stockrank commit [--run ID]`** (default: latest run with a finalized portfolio) shows `trades.csv` and the implied turnover, asks for confirmation, snapshots the current `live/portfolio.toml` into `live/history/<timestamp>.toml`, then writes the new one. Refuses if the run's portfolio isn't finalized; warns if that run was already committed.
- **The loop this creates:** the next run's `prepare portfolio` renders the committed book into `current.csv` (ticker, weight, rationale) and `current.md` (provenance header: built when, by which strategy with which mode weights, and the thesis), so the allocator manages *from* the book — including knowing under which market lens it was constructed — rather than rebuilding — continuity is carried by state (current book) and policy (turnover discipline in the portfolio mode's `task`), while scores and proposals stay deliberately stateless and fresh each run.
- Broker execution of `trades.csv` remains the user's manual job (see non-goals).

## 11. Tech stack

Python ≥ 3.11, `pyproject.toml` with a `stockrank` console script. Dependencies: `typer` (CLI), `rich` (progress bars for downloads, tables for `config`, colored finalize warnings, and the numbered strategy picker via `rich.prompt`), `httpx`, `pandas`, `python-dotenv`, `tomli-w` (stdlib `tomllib` for reading). Every command is run-print-exit and stays pipeable and scriptable around the external harness workflow. No database, no daemon, no state outside `runs/` and the TOML config files.

## 12. Explicit non-goals

- The CLI never invokes any LLM or agent harness.
- No backtesting (historical simulation), no broker integration, no scheduling. Forward tracking (§9) is the only performance feature.
- No per-run weight or selection overrides via flags — a run is fully defined by its strategy file. Want a different setup? Create another strategy TOML.
