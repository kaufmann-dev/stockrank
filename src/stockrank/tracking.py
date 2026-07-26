from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .runs import RunError, RunManifest, RunPaths, RunStore, atomic_write_csv, atomic_write_json, read_csv


class CloseClient(Protocol):
    def fetch_adjusted_closes(self, ticker: str, start: date, end: date) -> Mapping[date, float]:
        """Return split-adjusted closes; values must not include dividends."""
        ...


class TrackingError(RunError):
    """Raised when a finalized ranking cannot be tracked reliably."""


@dataclass(frozen=True)
class RankedTicker:
    ticker: str
    rank: int
    quantile: int


@dataclass(frozen=True)
class TickerPerformance:
    ticker: str
    rank: int
    quantile: int
    price_return: float
    benchmark_relative_return: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "quantile": self.quantile,
            "price_return": self.price_return,
            "benchmark_relative_return": self.benchmark_relative_return,
        }


@dataclass(frozen=True)
class QuantilePerformance:
    quantile: int
    count: int
    price_return: float
    benchmark_relative_return: float

    def to_dict(self) -> dict[str, object]:
        return {
            "quantile": self.quantile,
            "count": self.count,
            "price_return": self.price_return,
            "benchmark_relative_return": self.benchmark_relative_return,
        }


@dataclass(frozen=True)
class TrackingReport:
    run_id: str
    inception: date
    through: date
    benchmark: str
    benchmark_return: float
    ticker_returns: tuple[TickerPerformance, ...]
    quantile_returns: tuple[QuantilePerformance, ...]
    top_minus_bottom: float
    spearman_rank_correlation: float | None
    performance_csv: Path
    performance_json: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "inception": self.inception.isoformat(),
            "through": self.through.isoformat(),
            "return_type": "split-adjusted price return without dividends",
            "benchmark": self.benchmark,
            "benchmark_return": self.benchmark_return,
            "ticker_returns": [row.to_dict() for row in self.ticker_returns],
            "quantile_returns": [row.to_dict() for row in self.quantile_returns],
            "top_minus_bottom": self.top_minus_bottom,
            "spearman_rank_correlation": self.spearman_rank_correlation,
        }


_PERFORMANCE_FIELDS = (
    "date",
    "record_type",
    "identifier",
    "rank",
    "quantile",
    "count",
    "price_return",
    "benchmark",
    "benchmark_return",
    "benchmark_relative_return",
    "top_minus_bottom",
    "spearman_rank_correlation",
)


def track_run(
    root: Path,
    run_id: str,
    close_client: CloseClient,
    benchmark: str = "SPY",
    *,
    end_date: date | None = None,
) -> TrackingReport:
    """Track a completed ranking through an inclusive market date.

    The first observation is the earliest post-completion session for which the
    benchmark and every ranked ticker have a close. Once selected, that
    inception is frozen in the run manifest.
    """

    store = RunStore(root)
    manifest = store.load(run_id)
    _require_trackable(manifest)
    paths = store.paths(run_id)
    ranked = _load_ranking(paths)
    benchmark_symbol = _ticker(benchmark, "benchmark")
    completion = _completion_time(manifest)
    first_possible = completion.date() + timedelta(days=1)
    through = end_date or datetime.now(UTC).date()
    if through < first_possible:
        raise TrackingError(
            f"run {run_id} completed on {completion.date().isoformat()}; "
            f"tracking cannot end before {first_possible.isoformat()}"
        )

    symbols = tuple(dict.fromkeys([*(item.ticker for item in ranked), benchmark_symbol]))
    closes = {
        symbol: _normalized_closes(
            close_client.fetch_adjusted_closes(symbol, first_possible, through),
            symbol,
            first_possible,
            through,
        )
        for symbol in symbols
    }
    inception = _resolve_inception(manifest, closes, symbols, first_possible, through)
    if manifest.tracking_inception is None:
        store.set_tracking_inception(run_id, inception)

    market_dates = sorted(day for day in closes[benchmark_symbol] if inception <= day <= through)
    if not market_dates:
        raise TrackingError(
            f"benchmark {benchmark_symbol} has no closes from {inception.isoformat()} through {through.isoformat()}"
        )
    if market_dates[0] != inception:
        raise TrackingError(
            f"frozen inception {inception.isoformat()} is not available for benchmark {benchmark_symbol}"
        )
    bases = _base_closes(closes, symbols, inception)
    quantile_members = _quantile_members(ranked)
    rows: list[dict[str, object]] = []
    latest_tickers: tuple[TickerPerformance, ...] = ()
    latest_quantiles: tuple[QuantilePerformance, ...] = ()
    latest_benchmark_return = 0.0
    latest_spread = 0.0
    latest_correlation: float | None = None
    last_closes = dict(bases)

    for market_day in market_dates:
        for symbol in symbols:
            price = closes[symbol].get(market_day)
            if price is not None:
                last_closes[symbol] = price
        benchmark_return = last_closes[benchmark_symbol] / bases[benchmark_symbol] - 1.0
        ticker_returns = {item.ticker: last_closes[item.ticker] / bases[item.ticker] - 1.0 for item in ranked}
        ticker_rows = tuple(
            TickerPerformance(
                ticker=item.ticker,
                rank=item.rank,
                quantile=item.quantile,
                price_return=ticker_returns[item.ticker],
                benchmark_relative_return=ticker_returns[item.ticker] - benchmark_return,
            )
            for item in ranked
        )
        quantile_rows = tuple(
            QuantilePerformance(
                quantile=quantile,
                count=len(members),
                price_return=_mean([ticker_returns[item.ticker] for item in members]),
                benchmark_relative_return=(
                    _mean([ticker_returns[item.ticker] for item in members]) - benchmark_return
                ),
            )
            for quantile, members in quantile_members.items()
        )
        spread = quantile_rows[0].price_return - quantile_rows[-1].price_return
        correlation = _spearman(
            [float(item.rank) for item in ranked],
            [ticker_returns[item.ticker] for item in ranked],
        )
        rows.extend(
            _day_rows(
                market_day,
                benchmark_symbol,
                benchmark_return,
                ticker_rows,
                quantile_rows,
                spread,
                correlation,
            )
        )
        latest_tickers = ticker_rows
        latest_quantiles = quantile_rows
        latest_benchmark_return = benchmark_return
        latest_spread = spread
        latest_correlation = correlation

    report = TrackingReport(
        run_id=run_id,
        inception=inception,
        through=market_dates[-1],
        benchmark=benchmark_symbol,
        benchmark_return=latest_benchmark_return,
        ticker_returns=latest_tickers,
        quantile_returns=latest_quantiles,
        top_minus_bottom=latest_spread,
        spearman_rank_correlation=latest_correlation,
        performance_csv=paths.performance_csv,
        performance_json=paths.performance_json,
    )
    atomic_write_csv(paths.performance_csv, _PERFORMANCE_FIELDS, rows)
    atomic_write_json(paths.performance_json, report.to_dict())
    return report


def track_all(
    root: Path,
    close_client: CloseClient,
    benchmark: str = "SPY",
    *,
    end_date: date | None = None,
) -> list[TrackingReport]:
    store = RunStore(root)
    return [
        track_run(root, manifest.id, close_client, benchmark, end_date=end_date)
        for manifest in reversed(store.list(status="completed"))
    ]


def _require_trackable(manifest: RunManifest) -> None:
    if manifest.status != "completed" or manifest.completed_at is None:
        raise TrackingError(f"run {manifest.id} must be completed before tracking")


def _completion_time(manifest: RunManifest) -> datetime:
    assert manifest.completed_at is not None
    try:
        value = datetime.fromisoformat(manifest.completed_at)
    except ValueError as exc:
        raise TrackingError(f"run {manifest.id} has an invalid completion timestamp") from exc
    if value.tzinfo is None:
        raise TrackingError(f"run {manifest.id} completion timestamp has no timezone")
    return value.astimezone(UTC)


def _load_ranking(paths: RunPaths) -> tuple[RankedTicker, ...]:
    raw = read_csv(paths.ranking_csv, required_fields=("rank", "ticker"))
    if not raw:
        raise TrackingError(f"ranking is empty: {paths.ranking_csv}")
    pairs: list[tuple[int, str]] = []
    for index, row in enumerate(raw, start=2):
        try:
            rank = int(row["rank"])
        except (TypeError, ValueError) as exc:
            raise TrackingError(f"{paths.ranking_csv}:{index}: rank must be an integer") from exc
        ticker = _ticker(row["ticker"], f"{paths.ranking_csv}:{index} ticker")
        pairs.append((rank, ticker))
    ranks = [rank for rank, _ in pairs]
    tickers = [ticker for _, ticker in pairs]
    if sorted(ranks) != list(range(1, len(pairs) + 1)):
        raise TrackingError("ranking ranks must be unique and contiguous from 1")
    if len(tickers) != len(set(tickers)):
        raise TrackingError("ranking contains duplicate tickers")
    pairs.sort()
    quantile_count = 5 if len(pairs) >= 20 else min(3, len(pairs))
    assignments: dict[str, int] = {}
    offset = 0
    base_size, larger_groups = divmod(len(pairs), quantile_count)
    for quantile in range(1, quantile_count + 1):
        size = base_size + (1 if quantile <= larger_groups else 0)
        for _, ticker in pairs[offset : offset + size]:
            assignments[ticker] = quantile
        offset += size
    return tuple(
        RankedTicker(ticker=ticker, rank=rank, quantile=assignments[ticker]) for rank, ticker in pairs
    )


def _ticker(value: str, label: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not normalized or any(character in normalized for character in ("/", "\\", "\0")):
        raise TrackingError(f"{label} must be a valid non-empty ticker")
    return normalized


def _normalized_closes(
    raw: Mapping[date, float],
    ticker: str,
    start: date,
    end: date,
) -> dict[date, float]:
    if not isinstance(raw, Mapping):
        raise TrackingError(f"close client returned a non-mapping value for {ticker}")
    normalized: dict[date, float] = {}
    for day, raw_price in raw.items():
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TrackingError(f"close client returned a non-date key for {ticker}")
        if day < start or day > end:
            continue
        if isinstance(raw_price, bool) or not isinstance(raw_price, int | float):
            raise TrackingError(f"close client returned a non-numeric close for {ticker} on {day}")
        price = float(raw_price)
        if not math.isfinite(price) or price <= 0:
            raise TrackingError(f"close client returned an invalid close for {ticker} on {day}")
        normalized[day] = price
    return normalized


def _resolve_inception(
    manifest: RunManifest,
    closes: Mapping[str, Mapping[date, float]],
    symbols: Sequence[str],
    first_possible: date,
    through: date,
) -> date:
    if manifest.tracking_inception is not None:
        inception = date.fromisoformat(manifest.tracking_inception)
        if inception < first_possible:
            raise TrackingError(
                f"run {manifest.id} has inception {inception.isoformat()} before its post-completion window"
            )
        if inception > through:
            raise TrackingError(
                f"run {manifest.id} inception {inception.isoformat()} is after tracking end {through.isoformat()}"
            )
        return inception
    common_dates: set[date] | None = None
    for symbol in symbols:
        available = {day for day in closes[symbol] if first_possible <= day <= through}
        common_dates = available if common_dates is None else common_dates & available
    if not common_dates:
        missing = ", ".join(symbol for symbol in symbols if not closes[symbol])
        detail = f"; no data for {missing}" if missing else ""
        raise TrackingError(f"no common post-completion session is available for the ranking{detail}")
    return min(common_dates)


def _base_closes(
    closes: Mapping[str, Mapping[date, float]],
    symbols: Sequence[str],
    inception: date,
) -> dict[str, float]:
    missing = [symbol for symbol in symbols if inception not in closes[symbol]]
    if missing:
        raise TrackingError(
            f"frozen inception {inception.isoformat()} has no close for: {', '.join(sorted(missing))}"
        )
    return {symbol: closes[symbol][inception] for symbol in symbols}


def _quantile_members(ranked: Sequence[RankedTicker]) -> dict[int, tuple[RankedTicker, ...]]:
    quantiles = sorted({item.quantile for item in ranked})
    return {quantile: tuple(item for item in ranked if item.quantile == quantile) for quantile in quantiles}


def _day_rows(
    market_day: date,
    benchmark: str,
    benchmark_return: float,
    ticker_rows: Sequence[TickerPerformance],
    quantile_rows: Sequence[QuantilePerformance],
    spread: float,
    correlation: float | None,
) -> list[dict[str, object]]:
    day = market_day.isoformat()
    rows: list[dict[str, object]] = []
    for item in ticker_rows:
        rows.append(
            {
                "date": day,
                "record_type": "ticker",
                "identifier": item.ticker,
                "rank": item.rank,
                "quantile": item.quantile,
                "count": 1,
                "price_return": _format_number(item.price_return),
                "benchmark": benchmark,
                "benchmark_return": _format_number(benchmark_return),
                "benchmark_relative_return": _format_number(item.benchmark_relative_return),
                "top_minus_bottom": None,
                "spearman_rank_correlation": None,
            }
        )
    for item in quantile_rows:
        rows.append(
            {
                "date": day,
                "record_type": "quantile",
                "identifier": f"Q{item.quantile}",
                "rank": None,
                "quantile": item.quantile,
                "count": item.count,
                "price_return": _format_number(item.price_return),
                "benchmark": benchmark,
                "benchmark_return": _format_number(benchmark_return),
                "benchmark_relative_return": _format_number(item.benchmark_relative_return),
                "top_minus_bottom": None,
                "spearman_rank_correlation": None,
            }
        )
    rows.append(
        {
            "date": day,
            "record_type": "summary",
            "identifier": "ranking",
            "rank": None,
            "quantile": None,
            "count": len(ticker_rows),
            "price_return": None,
            "benchmark": benchmark,
            "benchmark_return": _format_number(benchmark_return),
            "benchmark_relative_return": None,
            "top_minus_bottom": _format_number(spread),
            "spearman_rank_correlation": None if correlation is None else _format_number(correlation),
        }
    )
    return rows


def _format_number(value: float) -> str:
    return f"{value:.10f}"


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise TrackingError("cannot compute an empty quantile return")
    return math.fsum(values) / len(values)


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right):
        raise TrackingError("Spearman inputs must have equal lengths")
    if len(left) < 2:
        return None
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = _mean(left_ranks)
    right_mean = _mean(right_ranks)
    covariance = math.fsum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks, strict=True)
    )
    left_variance = math.fsum((value - left_mean) ** 2 for value in left_ranks)
    right_variance = math.fsum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0:
        return None
    return covariance / denominator


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    offset = 0
    while offset < len(order):
        end = offset + 1
        while end < len(order) and values[order[end]] == values[order[offset]]:
            end += 1
        average = ((offset + 1) + end) / 2.0
        for index in order[offset:end]:
            ranks[index] = average
        offset = end
    return ranks
