from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.table import Table

from .config import ConfigError, is_finalized, latest_run, load_global_config, load_run_manifest, read_toml, run_ids
from .massive import MassiveClient


class CloseClient(Protocol):
    def fetch_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        ...


@dataclass(frozen=True)
class WeightedBook:
    name: str
    weights: dict[str, float]


def track_run(
    root: Path,
    *,
    run_id: str | None = None,
    client: CloseClient | None = None,
    console: Console | None = None,
    today: date | None = None,
) -> str:
    console = console or Console()
    selected = run_id or latest_run(root, "portfolio")
    manifest = load_run_manifest(root, selected)
    inception_raw = manifest.get("inception")
    if not isinstance(inception_raw, str) or not inception_raw:
        raise ConfigError(f"run {selected} has no inception date; finalize portfolio first")
    inception = date.fromisoformat(inception_raw)
    end = today or datetime.now(UTC).date()
    global_config = load_global_config(root)
    close_client = client or MassiveClient.from_env(root)
    run_root = root / "runs" / selected
    books = [WeightedBook("portfolio", _load_weights(run_root / "portfolio" / "portfolio.csv"))]
    books.extend(_load_proposal_books(run_root / "proposals" / "proposals.csv"))
    tickers = sorted({ticker for book in books for ticker in book.weights} | {global_config.benchmark})
    closes = {ticker: close_client.fetch_closes(ticker, inception, end) for ticker in tickers}
    dates = _output_dates(closes, inception, end)
    warnings: list[str] = []
    benchmark = _book_series(WeightedBook("benchmark", {global_config.benchmark: 1.0}), closes, dates, inception, warnings)
    series = {book.name: _book_series(book, closes, dates, inception, warnings) for book in books}
    rows: list[dict[str, str]] = []
    for index, day in enumerate(dates):
        row = {"date": day.isoformat(), "benchmark": f"{benchmark[index]:.6f}"}
        for name, values in series.items():
            row[name] = f"{values[index]:.6f}"
        rows.append(row)
    fieldnames = ["date", "benchmark", *[book.name for book in books]]
    _write_csv(run_root / "portfolio" / "performance.csv", fieldnames, rows)
    for warning in warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    _print_run_summary(console, selected, rows)
    return selected


def track_all(root: Path, *, client: CloseClient | None = None, console: Console | None = None, today: date | None = None) -> list[str]:
    console = console or Console()
    selected = [run_id for run_id in run_ids(root) if is_finalized(root, run_id, "portfolio")]
    if not selected:
        raise ConfigError("no finalized portfolios found")
    for run_id in selected:
        track_run(root, run_id=run_id, client=client, console=console, today=today)
    table = Table(title="Run Performance")
    for column in ("run", "inception", "portfolio_return", "benchmark_return", "excess"):
        table.add_column(column)
    for run_id in selected:
        manifest = load_run_manifest(root, run_id)
        rows = _read_rows(root / "runs" / run_id / "portfolio" / "performance.csv")
        if not rows:
            continue
        last = rows[-1]
        portfolio_return = float(last["portfolio"]) - 1.0
        benchmark_return = float(last["benchmark"]) - 1.0
        table.add_row(run_id, str(manifest.get("inception", "")), f"{portfolio_return:.6f}", f"{benchmark_return:.6f}", f"{portfolio_return - benchmark_return:.6f}")
    console.print(table)
    return selected


def track_live(
    root: Path,
    *,
    client: CloseClient | None = None,
    console: Console | None = None,
    today: date | None = None,
) -> None:
    console = console or Console()
    live_files = sorted((root / "live" / "history").glob("*.toml")) if (root / "live" / "history").exists() else []
    current = root / "live" / "portfolio.toml"
    if current.exists() and current.stat().st_size > 0:
        live_files.append(current)
    if not live_files:
        raise ConfigError("no live portfolio commits found")
    snapshots = sorted((read_toml(path) for path in live_files), key=lambda raw: str(raw.get("committed", "")))
    global_config = load_global_config(root)
    close_client = client or MassiveClient.from_env(root)
    end = today or datetime.now(UTC).date()
    rows: list[dict[str, str]] = []
    chain_live = 1.0
    chain_benchmark = 1.0
    for index, snapshot in enumerate(snapshots):
        start = _date_from_timestamp(str(snapshot["committed"]))
        segment_end = end
        if index + 1 < len(snapshots):
            segment_end = _date_from_timestamp(str(snapshots[index + 1]["committed"])) - timedelta(days=1)
        if segment_end < start:
            continue
        weights = {str(p["ticker"]).upper(): float(p["weight"]) for p in snapshot.get("positions", []) or []}
        tickers = sorted(set(weights) | {global_config.benchmark})
        closes = {ticker: close_client.fetch_closes(ticker, start, segment_end) for ticker in tickers}
        dates = _output_dates(closes, start, segment_end)
        warnings: list[str] = []
        live_values = _book_series(WeightedBook("live", weights), closes, dates, start, warnings)
        benchmark_values = _book_series(WeightedBook("benchmark", {global_config.benchmark: 1.0}), closes, dates, start, warnings)
        for i, day in enumerate(dates):
            rows.append(
                {
                    "date": day.isoformat(),
                    "benchmark": f"{chain_benchmark * benchmark_values[i]:.6f}",
                    "live": f"{chain_live * live_values[i]:.6f}",
                }
            )
        if live_values:
            chain_live *= live_values[-1]
        if benchmark_values:
            chain_benchmark *= benchmark_values[-1]
        for warning in warnings:
            console.print(f"[yellow]warning:[/] {warning}")
    _write_csv(root / "live" / "performance.csv", ["date", "benchmark", "live"], rows)
    if rows:
        console.print(f"Live return: {float(rows[-1]['live']) - 1.0:.6f}")


def _date_from_timestamp(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _load_weights(path: Path) -> dict[str, float]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["ticker"].upper(): float(row["weight"]) for row in csv.DictReader(fh)}


def _load_proposal_books(path: Path) -> list[WeightedBook]:
    if not path.exists():
        return []
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            grouped[f"{row['mode']}__{row['harness']}"][row["ticker"].upper()] = float(row["weight"])
    return [WeightedBook(name, weights) for name, weights in sorted(grouped.items())]


def _output_dates(closes: dict[str, dict[date, float]], start: date, end: date) -> list[date]:
    dates = sorted({day for ticker_closes in closes.values() for day in ticker_closes if start <= day <= end})
    return dates or [start]


def _book_series(
    book: WeightedBook,
    closes: dict[str, dict[date, float]],
    dates: list[date],
    inception: date,
    warnings: list[str],
) -> list[float]:
    bases: dict[str, float] = {}
    last: dict[str, float] = {}
    for ticker in book.weights:
        ticker_closes = closes.get(ticker, {})
        base_day = next((day for day in sorted(ticker_closes) if day >= inception), None)
        if base_day is None:
            warnings.append(f"{book.name}: no prices for {ticker}; carrying zero contribution")
            bases[ticker] = 0.0
            last[ticker] = 0.0
            continue
        if base_day > inception:
            warnings.append(f"{book.name}: {ticker} missing inception close; using {base_day.isoformat()}")
        bases[ticker] = ticker_closes[base_day]
        last[ticker] = ticker_closes[base_day]
    values: list[float] = []
    for day in dates:
        total = 0.0
        for ticker, weight in book.weights.items():
            if day in closes.get(ticker, {}):
                last[ticker] = closes[ticker][day]
            elif ticker in bases and bases[ticker] > 0:
                warnings.append(f"{book.name}: {ticker} carried last close on {day.isoformat()}")
            if bases.get(ticker, 0.0) > 0:
                total += weight * (last[ticker] / bases[ticker])
        values.append(total)
    return values


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _print_run_summary(console: Console, run_id: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    last = rows[-1]
    console.print(
        f"Run {run_id}: portfolio {float(last['portfolio']) - 1.0:.6f}, "
        f"benchmark {float(last['benchmark']) - 1.0:.6f}"
    )
