from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from stockrank.runs import (
    RunStore,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    read_json,
)
from stockrank.tracking import TrackingError, track_all, track_run


class FakeCloseClient:
    def __init__(self, closes: dict[str, dict[date, float]]) -> None:
        self.closes = closes
        self.calls: list[tuple[str, date, date]] = []

    def fetch_adjusted_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        self.calls.append((ticker, start, end))
        return self.closes.get(ticker, {})


def _completed_run(root: Path, *, count: int = 6, second: int = 0) -> tuple[RunStore, str]:
    store = RunStore(root)
    manifest = store.create(
        universe={"name": "test", "tickers": [f"T{index:02d}" for index in range(1, count + 1)]},
        mode={"name": "best-bet"},
        model={"name": "fake"},
        settings={"data": {"minimum_bars": 60}},
        profile="medium",
        seed=7,
        now=datetime(2026, 7, 24, 10, 0, second, tzinfo=UTC),
    )
    atomic_write_csv(
        store.paths(manifest.id).ranking_csv,
        ("rank", "ticker", "strength"),
        (
            {"rank": index, "ticker": f"T{index:02d}", "strength": count - index}
            for index in range(1, count + 1)
        ),
    )
    atomic_write_json(
        store.paths(manifest.id).ranking_json,
        {"ranking": [{"rank": index, "ticker": f"T{index:02d}"} for index in range(1, count + 1)]},
    )
    atomic_write_text(store.paths(manifest.id).report, "# Ranking\n")
    store.complete(manifest.id, now=datetime(2026, 7, 24, 15, 0, second, tzinfo=UTC))
    return store, manifest.id


def test_tracking_uses_post_completion_session_and_computes_rank_metrics(tmp_path: Path) -> None:
    store, run_id = _completed_run(tmp_path)
    monday = date(2026, 7, 27)
    tuesday = date(2026, 7, 28)
    closes = {
        f"T{index:02d}": {
            monday: 100.0,
            tuesday: 100.0 * (1.0 + (0.7 - index * 0.1)),
        }
        for index in range(1, 7)
    }
    closes["SPY"] = {monday: 200.0, tuesday: 210.0}
    client = FakeCloseClient(closes)

    report = track_run(tmp_path, run_id, client, "spy", end_date=tuesday)

    assert report.inception == monday
    assert report.through == tuesday
    assert report.benchmark == "SPY"
    assert report.benchmark_return == pytest.approx(0.05)
    assert [item.count for item in report.quantile_returns] == [2, 2, 2]
    assert [item.price_return for item in report.quantile_returns] == pytest.approx([0.55, 0.35, 0.15])
    assert report.top_minus_bottom == pytest.approx(0.4)
    assert report.spearman_rank_correlation == pytest.approx(-1.0)
    assert report.ticker_returns[0].benchmark_relative_return == pytest.approx(0.55)
    assert store.load(run_id).tracking_inception == monday.isoformat()
    assert all(call[1] == date(2026, 7, 25) for call in client.calls)

    with report.performance_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 20
    final_summary = rows[-1]
    assert final_summary["record_type"] == "summary"
    assert final_summary["top_minus_bottom"] == "0.4000000000"
    assert final_summary["spearman_rank_correlation"] == "-1.0000000000"
    assert read_json(report.performance_json)["return_type"] == (
        "split-adjusted price return without dividends"
    )


def test_tracking_is_byte_idempotent_and_keeps_frozen_inception(tmp_path: Path) -> None:
    store, run_id = _completed_run(tmp_path)
    monday = date(2026, 7, 27)
    closes = {f"T{index:02d}": {monday: float(index)} for index in range(1, 7)}
    closes["SPY"] = {monday: 100.0}
    client = FakeCloseClient(closes)

    first = track_run(tmp_path, run_id, client, end_date=monday)
    csv_bytes = first.performance_csv.read_bytes()
    json_bytes = first.performance_json.read_bytes()
    lifecycle = store.load(run_id).lifecycle
    second = track_run(tmp_path, run_id, client, end_date=monday)

    assert second.performance_csv.read_bytes() == csv_bytes
    assert second.performance_json.read_bytes() == json_bytes
    assert store.load(run_id).lifecycle == lifecycle


def test_tracking_waits_for_first_common_session(tmp_path: Path) -> None:
    store, run_id = _completed_run(tmp_path)
    monday = date(2026, 7, 27)
    tuesday = date(2026, 7, 28)
    closes = {f"T{index:02d}": {monday: 100.0, tuesday: 101.0} for index in range(1, 7)}
    closes["T06"] = {tuesday: 101.0}
    closes["SPY"] = {monday: 100.0, tuesday: 101.0}

    report = track_run(tmp_path, run_id, FakeCloseClient(closes), end_date=tuesday)

    assert report.inception == tuesday
    assert report.benchmark_return == 0.0
    assert store.load(run_id).tracking_inception == tuesday.isoformat()


def test_tracking_rejects_missing_common_prices(tmp_path: Path) -> None:
    _, run_id = _completed_run(tmp_path)
    monday = date(2026, 7, 27)
    closes = {f"T{index:02d}": {monday: 100.0} for index in range(1, 6)}
    closes["SPY"] = {monday: 100.0}

    with pytest.raises(TrackingError, match="no common post-completion session"):
        track_run(tmp_path, run_id, FakeCloseClient(closes), end_date=monday)


def test_twenty_names_use_five_equal_count_quantiles_and_track_all(tmp_path: Path) -> None:
    _, first = _completed_run(tmp_path, count=20, second=1)
    _, second = _completed_run(tmp_path, count=20, second=2)
    monday = date(2026, 7, 27)
    closes = {f"T{index:02d}": {monday: 100.0} for index in range(1, 21)}
    closes["SPY"] = {monday: 100.0}

    reports = track_all(tmp_path, FakeCloseClient(closes), end_date=monday)

    assert [report.run_id for report in reports] == [first, second]
    assert [[item.count for item in report.quantile_returns] for report in reports] == [
        [4, 4, 4, 4, 4],
        [4, 4, 4, 4, 4],
    ]
