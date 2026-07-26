from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from stockrank.massive import MassiveResult
from stockrank.universe import (
    LiquidityUniverseSpec,
    StaticUniverseSpec,
    UniverseError,
    load_universe_spec,
    resolve_universe,
)


class _FakeMassive:
    def get_ticker_details(self, ticker: str, as_of: date | None = None) -> MassiveResult:
        return MassiveResult(value={"ticker": ticker, "active": ticker != "OLD"})

    def list_tickers(self, as_of: date) -> MassiveResult:
        return MassiveResult(
            records=(
                {
                    "ticker": "AAA",
                    "active": True,
                    "locale": "us",
                    "market": "stocks",
                    "type": "CS",
                    "primary_exchange": "XNYS",
                },
                {
                    "ticker": "BBB",
                    "active": True,
                    "locale": "us",
                    "market": "stocks",
                    "type": "CS",
                    "primary_exchange": "XNAS",
                },
                {
                    "ticker": "LOW",
                    "active": True,
                    "locale": "us",
                    "market": "stocks",
                    "type": "CS",
                    "primary_exchange": "XNYS",
                },
                {
                    "ticker": "ETF",
                    "active": True,
                    "locale": "us",
                    "market": "stocks",
                    "type": "ETF",
                    "primary_exchange": "XNYS",
                },
            )
        )

    def get_grouped_daily(self, day: date) -> MassiveResult:
        return MassiveResult(
            records=(
                {"T": "AAA", "c": 20.0, "v": 100.0},
                {"T": "BBB", "c": 10.0, "v": 300.0},
                {"T": "LOW", "c": 2.0, "v": 10_000.0},
            )
        )


def test_load_exact_static_and_liquidity_schemas(tmp_path: Path) -> None:
    static_path = tmp_path / "static.toml"
    static_path.write_text(
        'name = "Ideas"\nkind = "static"\ntickers = ["aapl", "MSFT"]\n',
        encoding="utf-8",
    )
    static = load_universe_spec(static_path)
    assert static == StaticUniverseSpec(name="Ideas", kind="static", tickers=("AAPL", "MSFT"))

    liquidity_path = tmp_path / "liquidity.toml"
    liquidity_path.write_text(
        """
name = "Liquid 2"
kind = "liquidity"
top_n = 2
lookback_sessions = 20
min_price = 5.0
exchanges = ["XNYS", "XNAS"]
""",
        encoding="utf-8",
    )
    liquidity = load_universe_spec(liquidity_path)
    assert liquidity == LiquidityUniverseSpec(
        name="Liquid 2",
        kind="liquidity",
        top_n=2,
        lookback_sessions=20,
        min_price=5.0,
        exchanges=("XNYS", "XNAS"),
    )

    static_path.write_text(
        'name = "Bad"\nkind = "static"\ntickers = ["AAA"]\nlegacy = true\n',
        encoding="utf-8",
    )
    with pytest.raises(UniverseError, match="unknown fields"):
        load_universe_spec(static_path)


def test_static_resolution_validates_and_freezes_declared_order() -> None:
    client = _FakeMassive()
    spec = StaticUniverseSpec(name="Ideas", kind="static", tickers=("BBB", "AAA"))
    frozen = resolve_universe(spec, client, date(2026, 7, 25))
    assert frozen.tickers == ("BBB", "AAA")
    assert frozen.as_dict()["as_of"] == "2026-07-25"

    with pytest.raises(UniverseError, match="inactive"):
        resolve_universe(
            StaticUniverseSpec(name="Old", kind="static", tickers=("OLD",)),
            client,
            date(2026, 7, 25),
        )


def test_liquidity_resolution_uses_median_dollar_volume_and_stable_ties() -> None:
    spec = LiquidityUniverseSpec(
        name="Liquid",
        kind="liquidity",
        top_n=2,
        lookback_sessions=2,
        min_price=5.0,
        exchanges=("XNYS", "XNAS"),
    )
    frozen = resolve_universe(spec, _FakeMassive(), date(2026, 7, 24))
    assert frozen.tickers == ("BBB", "AAA")
    assert [item.sessions for item in frozen.liquidity] == [2, 2]
    assert any(item.ticker == "LOW" and "minimum price" in item.reason for item in frozen.exclusions)
