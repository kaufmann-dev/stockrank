# stockrank

`stockrank` is a personal research CLI that turns fresh public evidence into a complete relative
ranking of a chosen U.S. equity universe.

It retrieves market and filing evidence from Massive and SEC EDGAR, builds compact normalized
evidence packs, asks one configured OpenAI-compatible model to rank balanced groups of stocks, and
fits those observations with a regularized Plackett–Luce model. Every run freezes its inputs and
stores the evidence, cited judgments, uncertainty diagnostics, and forward results needed to audit
what happened.

This is research software, not investment advice.

## Product boundaries

- The model compares stocks in small groups; it never assigns isolated 0–100 scores.
- The CLI performs data preparation, scheduling, validation, fitting, and tracking deterministically.
- The built-in `best-bet` lens ranks investments rather than business quality. Additional objectives
  are saved as explicit TOML mode files.
- `stockrank` ranks complete universes. It does not construct portfolios, place trades, backtest old
  dates, invoke external agent harnesses, or manage a live book.
- Historical artifacts are audit inputs only. They never become evidence for a later ranking.

## Install

Use Python 3.11 or newer and `uv`:

```sh
uv sync --all-extras
uv tool install --editable . --force
cd /path/to/a/stockrank-project
stockrank init
```

The tool installation creates the global `stockrank` command while keeping it linked to this
checkout. `uv sync --all-extras` separately prepares the development environment. Installation does
not create project files: `stockrank init` explicitly initializes the current directory with a
configuration, the `best-bet` mode, and the `liquid-50` universe. Running it again in a valid
project leaves those project files unchanged and resumes the setup wizard. During partial
initialization, valid existing starter mode and universe files are preserved and only missing files
are created. The interactive setup then searches the current
[models.dev](https://models.dev/) catalog for an OpenAI-compatible provider and model, stores the
provider API key, stores the model profile in the user configuration directory, and stores the
Massive API key. If the catalog is unavailable or does not contain the provider, choose **Custom
OpenAI-compatible provider** and enter its base URL.

Credential prompts are hidden and require confirmation. Secrets are saved in the operating system
keyring; API keys are never stored in `stockrank.toml` or passed as command-line options. SEC requests
use the repository URL as their default application identity; `sources.sec.user_agent` can override
it when a different identity is appropriate.

## Configuration

`stockrank.toml` defines:

- Massive and SEC connection settings;
- evidence depth and minimum coverage;
- the default mode, race profile, and seed;
- the tracking benchmark;

Named OpenAI-compatible model profiles and the selected default model are user-wide settings. They
are saved at `$XDG_CONFIG_HOME/stockrank/config.toml`, or
`~/.config/stockrank/config.toml` when `XDG_CONFIG_HOME` is unset. The first profile configured by
`stockrank init` automatically becomes the default. Rerunning `stockrank init` offers to add another
profile and asks whether it should become the default; it does not replace existing profiles or
overwrite an existing Massive key.

A DeepSeek profile uses the standard OpenAI Python client with `https://api.deepseek.com`. Add and
securely authenticate any other OpenAI-compatible provider from the CLI:

```sh
stockrank model add openrouter \
  --provider openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model provider/model
stockrank model default openrouter
```

The non-secret profile is saved under `[models.<name>]` in the user configuration file with its
provider ID, base URL, and model ID; its API key is saved separately under the same profile name in
the operating system keyring. Each run selects exactly one profile and freezes its resolved
non-secret settings. Provider-specific controls such as DeepSeek thinking mode and
`reasoning_effort` remain explicit in that profile.

Modes live in `modes/*.toml` and contain `name`, `rank_1_meaning`, and one complete `prompt`.
`best-bet` is the only bundled mode. Custom ranking instructions belong in another saved mode file;
there are no one-off prompt flags.

Universes live in `universes/*.toml`:

- `kind = "static"` uses an explicit `tickers` list.
- `kind = "liquidity"` resolves active U.S. common stocks from Massive and chooses the configured
  `top_n` by trailing median dollar volume.

The bundled `liquid-50` definition is dynamic. A run freezes the resolved members before fetching
ranking evidence.

## Run a ranking

Inspect configuration:

```sh
stockrank universe list
stockrank universe show liquid-50
stockrank universe resolve liquid-50
stockrank mode show best-bet
stockrank model list
stockrank model status deepseek
stockrank model test deepseek
stockrank massive status
```

Start the default medium-quality ranking:

```sh
stockrank rank --universe liquid-50
```

Choose a saved mode, model profile, or race budget:

```sh
stockrank rank \
  --universe liquid-50 \
  --mode best-bet \
  --model deepseek \
  --profile high
```

Profiles control model-call cost:

- `low`: groups of six, four appearances per stock;
- `medium`: groups of five, eight appearances per stock;
- `high`: groups of four, twelve appearances per stock.

Runs checkpoint every source payload, evidence pack, and valid race response. Resume an interrupted
run without changing its frozen inputs:

```sh
stockrank rank --resume RUN_ID
```

Inspect stored runs:

```sh
stockrank runs list
stockrank runs show RUN_ID
```

## Evidence and ranking

Massive supplies ticker identity, adjusted daily bars, news, filing text and disclosures, insider
filings, short interest, dividends, and splits. SEC Company Facts supplies XBRL fundamentals without
requiring a paid Massive fundamentals add-on.

Raw responses are retained for audit. Prompt-visible evidence uses dated ratios, changes,
cross-sectional percentiles, and bounded filing/news excerpts with stable evidence IDs. A ticker
must have at least 60 daily bars and one substantive non-price evidence category; otherwise it is
reported as excluded.

Each model response must be an exact ticker permutation plus a cited, falsifiable judgment for every
stock. The CLI validates the response and permits one repair request. Failed observations are
replaced deterministically; finalization requires the included comparison graph to be connected.

The final order is the descending regularized Plackett–Luce strength. Seeded bootstrap resampling
adds rank intervals and rank deviation; it does not pretend to estimate a probability that the
investment thesis is true.

Finalized runs contain:

- `ranking.csv` for compact analysis;
- `ranking.json` for full structured output;
- `report.md` for a human digest;
- raw source, normalized evidence, schedule, and race artifacts.

## Forward tracking

Update one or all finalized runs:

```sh
stockrank track --run RUN_ID
stockrank track --all
```

Tracking uses split-adjusted Massive closes and reports price return without dividends. It records
per-ticker returns, equal-count rank quantiles, the top-minus-bottom spread, SPY-relative results,
and Spearman rank correlation. This is forward observation, not a historical backtest.

## Development

```sh
uv run pyright
uv run ruff check .
uv run pytest
```

Tests use fake HTTP/model clients by default. Live connectivity checks are explicit and should stay
small.
