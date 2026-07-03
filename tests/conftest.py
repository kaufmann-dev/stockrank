from __future__ import annotations

import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class FakeMassiveClient:
    def __init__(self, closes: dict[str, dict] | None = None) -> None:
        self.closes = closes or {}
        self.fetched: list[str] = []

    def fetch_many(self, tickers, data_config):
        self.fetched.extend(tickers)
        return {
            ticker: {
                "details.json": '{"results": {"ticker": "%s"}}' % ticker,
                "prices_daily.csv": "date,open,high,low,close,volume,vwap,transactions\n2026-01-02,1,1,1,1,1,1,1\n",
                "financials.json": '{"results": []}',
                "dividends.json": '{"results": []}',
                "splits.json": '{"results": []}',
                "news.json": '{"results": []}',
            }
            for ticker in tickers
        }

    def fetch_closes(self, ticker, start, end):
        return self.closes.get(ticker, {})


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    for dirname in ("prompts",):
        shutil.copytree(ROOT / dirname, tmp_path / dirname)
    for dirname in ("strategies", "modes", "harnesses", "universes", "live/history"):
        (tmp_path / dirname).mkdir(parents=True, exist_ok=True)
    (tmp_path / "stockrank.toml").write_text(
        """
[api]
provider = "massive"

[data]
price_history_years = 1
news_items = 2
financial_periods = 2

[defaults]
strategy = "mini"

[tracking]
benchmark = "SPY"
""",
        encoding="utf-8",
    )
    (tmp_path / "strategies" / "mini.toml").write_text(
        """
name = "mini"
universe = "mini"

[[scores.modes]]
name = "quality"
weight = 2.0
harnesses = [
  { name = "codex", weight = 1.0 },
  { name = "claude-code", weight = 3.0 },
]

[proposals]
num_positions = 2
runs = [
  { mode = "builder", harness = "codex" },
]

[portfolio]
mode = "allocator"
harness = "codex"
""",
        encoding="utf-8",
    )
    (tmp_path / "universes" / "mini.toml").write_text('name = "Mini"\ntickers = ["AAA", "BBB"]\n', encoding="utf-8")
    (tmp_path / "modes" / "quality.toml").write_text(
        'name = "quality"\nphase = "scores"\npersona = "score persona"\ntask = "score task"\n',
        encoding="utf-8",
    )
    (tmp_path / "modes" / "builder.toml").write_text(
        'name = "builder"\nphase = "proposals"\npersona = "build persona"\ntask = "build task"\n',
        encoding="utf-8",
    )
    (tmp_path / "modes" / "allocator.toml").write_text(
        'name = "allocator"\nphase = "portfolio"\npersona = "alloc persona"\ntask = "alloc task"\n',
        encoding="utf-8",
    )
    (tmp_path / "harnesses" / "codex.toml").write_text('name = "codex"\ndescription = "Codex"\n', encoding="utf-8")
    (tmp_path / "harnesses" / "claude-code.toml").write_text('name = "claude-code"\ndescription = "Claude"\n', encoding="utf-8")
    return tmp_path
