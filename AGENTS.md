# Repository Guidelines

## Product

`stockrank` is a Python CLI that ranks a selected U.S. equity universe end to end from fresh Massive
and SEC evidence. One configured OpenAI-compatible model ranks balanced small groups; regularized
Plackett–Luce produces the complete ranking; deterministic tracking measures future rank outcomes.

It is not a portfolio builder, broker, backtester, external-agent task system, or isolated stock
scorer.

## Structure

- `src/stockrank/` contains all runtime behavior; keep Typer functions in `cli.py` thin.
- `stockrank.toml` contains source, model-profile, ranking, and tracking configuration.
- `modes/` contains complete saved ranking objectives.
- `universes/` contains static or dynamic universe definitions.
- Ignored `runs/` contains immutable run inputs and resumable artifacts.

## Invariants

- Use current Massive endpoints. Never reintroduce the retired `/vX/reference/financials` endpoint.
- SEC requests declare the configured application User-Agent and remain below 10 requests per
  second.
- Prompt-visible evidence carries a public/knowledge date and stable evidence ID. Keep raw financial
  magnitudes in audit payloads; use ratios, changes, and cross-sectional views in evidence packs.
- Universe membership selects candidates and is frozen at run creation; it is not model evidence.
- A mode changes only the ranking objective. It never changes source fetching or ranking math.
- Every race response is an exact ticker permutation with locally validated evidence citations.
- Scheduling is seeded, balanced, connected, and contains no repeated ticker within a race.
- Primary rank sorts by unadjusted Plackett–Luce strength. Bootstrap output is diagnostic only.
- Model profiles use the OpenAI Chat Completions contract and parse `message.content`; do not add
  provider-specific clients.
- A resumed run uses its frozen universe, mode, model settings, schedule, and successful artifacts.
- Forward tracking never becomes input to a later ranking.
- API secrets live only in the operating system keyring; reports, TOML, and config displays never
  print secret values.

## Commands

```sh
uv sync --all-extras
uv run pyright
uv run ruff check .
uv run pytest
```

Tests use fake Massive, SEC, and model transports. Keep live checks explicit, bounded, and separate
from the default suite.
