from __future__ import annotations

import re
import statistics
import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

from .massive import MassiveResult


class UniverseError(RuntimeError):
    pass


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,31}$")


@dataclass(frozen=True)
class StaticUniverseSpec:
    name: str
    kind: Literal["static"]
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class LiquidityUniverseSpec:
    name: str
    kind: Literal["liquidity"]
    top_n: int
    lookback_sessions: int
    min_price: float
    exchanges: tuple[str, ...]


UniverseSpec: TypeAlias = StaticUniverseSpec | LiquidityUniverseSpec


class UniverseSource(Protocol):
    def get_ticker_details(self, ticker: str, as_of: date | None = None) -> MassiveResult: ...

    def list_tickers(self, as_of: date) -> MassiveResult: ...

    def get_grouped_daily(self, day: date) -> MassiveResult: ...


@dataclass(frozen=True)
class LiquidityObservation:
    ticker: str
    median_dollar_volume: float
    latest_close: float
    sessions: int

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "ticker": self.ticker,
            "median_dollar_volume": self.median_dollar_volume,
            "latest_close": self.latest_close,
            "sessions": self.sessions,
        }


@dataclass(frozen=True)
class UniverseExclusion:
    ticker: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"ticker": self.ticker, "reason": self.reason}


@dataclass(frozen=True)
class FrozenUniverse:
    name: str
    kind: Literal["static", "liquidity"]
    as_of: date
    tickers: tuple[str, ...]
    liquidity: tuple[LiquidityObservation, ...] = ()
    exclusions: tuple[UniverseExclusion, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "as_of": self.as_of.isoformat(),
            "tickers": list(self.tickers),
            "liquidity": [item.as_dict() for item in self.liquidity],
            "exclusions": [item.as_dict() for item in self.exclusions],
        }


def _required_string(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise UniverseError(f"{label} must be a non-empty string")
    return raw.strip()


def _positive_integer(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise UniverseError(f"{label} must be a positive integer")
    return raw


def _positive_number(raw: object, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not float(raw) > 0:
        raise UniverseError(f"{label} must be a positive number")
    return float(raw)


def _ticker(raw: object, label: str) -> str:
    ticker = _required_string(raw, label).upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise UniverseError(f"{label} is not a valid ticker symbol: {ticker!r}")
    return ticker


def load_universe_spec(path: Path) -> UniverseSpec:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise UniverseError(f"missing universe file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UniverseError(f"invalid TOML in {path}: {exc}") from exc

    name = _required_string(raw.get("name"), "universe.name")
    kind = _required_string(raw.get("kind"), "universe.kind")
    if kind == "static":
        allowed = {"name", "kind", "tickers"}
        unexpected = sorted(set(raw) - allowed)
        if unexpected:
            raise UniverseError(f"static universe contains unknown fields: {', '.join(unexpected)}")
        raw_tickers = raw.get("tickers")
        if not isinstance(raw_tickers, list) or not raw_tickers:
            raise UniverseError("static universe.tickers must be a non-empty array")
        tickers = tuple(
            _ticker(value, f"universe.tickers[{index}]") for index, value in enumerate(raw_tickers)
        )
        if len(tickers) != len(set(tickers)):
            raise UniverseError("static universe contains duplicate tickers")
        return StaticUniverseSpec(name=name, kind="static", tickers=tickers)

    if kind == "liquidity":
        allowed = {
            "name",
            "kind",
            "top_n",
            "lookback_sessions",
            "min_price",
            "exchanges",
        }
        unexpected = sorted(set(raw) - allowed)
        if unexpected:
            raise UniverseError(f"liquidity universe contains unknown fields: {', '.join(unexpected)}")
        exchanges_raw = raw.get("exchanges")
        if not isinstance(exchanges_raw, list) or not exchanges_raw:
            raise UniverseError("liquidity universe.exchanges must be a non-empty array")
        exchanges = tuple(
            _required_string(value, f"universe.exchanges[{index}]").upper()
            for index, value in enumerate(exchanges_raw)
        )
        if len(exchanges) != len(set(exchanges)):
            raise UniverseError("liquidity universe contains duplicate exchanges")
        return LiquidityUniverseSpec(
            name=name,
            kind="liquidity",
            top_n=_positive_integer(raw.get("top_n"), "universe.top_n"),
            lookback_sessions=_positive_integer(raw.get("lookback_sessions"), "universe.lookback_sessions"),
            min_price=_positive_number(raw.get("min_price"), "universe.min_price"),
            exchanges=exchanges,
        )

    raise UniverseError("universe.kind must be 'static' or 'liquidity'")


def _require_available(result: MassiveResult, label: str) -> MassiveResult:
    if result.coverage_issue is not None:
        raise UniverseError(
            f"cannot resolve universe: {label} is unavailable ({result.coverage_issue.message})"
        )
    return result


def resolve_universe(
    spec: UniverseSpec,
    massive_client: UniverseSource,
    as_of: date,
) -> FrozenUniverse:
    if isinstance(spec, StaticUniverseSpec):
        return _resolve_static(spec, massive_client, as_of)
    return _resolve_liquidity(spec, massive_client, as_of)


def _resolve_static(
    spec: StaticUniverseSpec,
    massive_client: UniverseSource,
    as_of: date,
) -> FrozenUniverse:
    validated: list[str] = []
    for ticker in spec.tickers:
        result = _require_available(
            massive_client.get_ticker_details(ticker, as_of),
            f"ticker details for {ticker}",
        )
        details = result.value or {}
        returned = str(details.get("ticker", "")).upper()
        if returned != ticker:
            raise UniverseError(f"Massive did not recognize static ticker {ticker}")
        if details.get("active") is False:
            raise UniverseError(f"static ticker {ticker} is inactive as of {as_of}")
        validated.append(ticker)
    return FrozenUniverse(
        name=spec.name,
        kind="static",
        as_of=as_of,
        tickers=tuple(validated),
    )


def _resolve_liquidity(
    spec: LiquidityUniverseSpec,
    massive_client: UniverseSource,
    as_of: date,
) -> FrozenUniverse:
    ticker_result = _require_available(massive_client.list_tickers(as_of), "active common-stock ticker list")
    exchange_set = set(spec.exchanges)
    candidates: set[str] = set()
    exclusions: list[UniverseExclusion] = []
    for raw in ticker_result.records:
        ticker_value = raw.get("ticker")
        if not isinstance(ticker_value, str):
            continue
        ticker = ticker_value.upper()
        if (
            raw.get("active") is not False
            and raw.get("locale") == "us"
            and raw.get("market") == "stocks"
            and raw.get("type") == "CS"
            and raw.get("primary_exchange") in exchange_set
            and _TICKER_RE.fullmatch(ticker)
        ):
            candidates.add(ticker)

    if not candidates:
        raise UniverseError("no active U.S. common stocks matched the exchanges")

    observations: dict[str, list[tuple[date, float, float]]] = {ticker: [] for ticker in candidates}
    completed_sessions = 0
    cursor = as_of
    maximum_calendar_days = spec.lookback_sessions * 3 + 31
    inspected_days = 0
    while completed_sessions < spec.lookback_sessions and inspected_days < maximum_calendar_days:
        grouped = _require_available(
            massive_client.get_grouped_daily(cursor),
            f"grouped daily bars for {cursor.isoformat()}",
        )
        rows_for_candidates = 0
        for row in grouped.records:
            raw_ticker = row.get("T") or row.get("ticker")
            close = row.get("c") if "c" in row else row.get("close")
            volume = row.get("v") if "v" in row else row.get("volume")
            if (
                not isinstance(raw_ticker, str)
                or raw_ticker.upper() not in candidates
                or isinstance(close, bool)
                or isinstance(volume, bool)
                or not isinstance(close, int | float)
                or not isinstance(volume, int | float)
                or close <= 0
                or volume < 0
            ):
                continue
            ticker = raw_ticker.upper()
            observations[ticker].append((cursor, float(close), float(close) * float(volume)))
            rows_for_candidates += 1
        if rows_for_candidates:
            completed_sessions += 1
        cursor -= timedelta(days=1)
        inspected_days += 1

    if completed_sessions < spec.lookback_sessions:
        raise UniverseError(
            f"only found {completed_sessions} market sessions before {as_of}; "
            f"{spec.lookback_sessions} required"
        )

    eligible: list[LiquidityObservation] = []
    for ticker in sorted(candidates):
        rows = sorted(observations[ticker], key=lambda item: item[0], reverse=True)
        if len(rows) < spec.lookback_sessions:
            exclusions.append(UniverseExclusion(ticker, "insufficient grouped daily bars"))
            continue
        window = rows[: spec.lookback_sessions]
        latest_close = window[0][1]
        if latest_close < spec.min_price:
            exclusions.append(UniverseExclusion(ticker, "latest close below minimum price"))
            continue
        eligible.append(
            LiquidityObservation(
                ticker=ticker,
                median_dollar_volume=float(statistics.median(row[2] for row in window)),
                latest_close=latest_close,
                sessions=len(window),
            )
        )

    eligible.sort(key=lambda item: (-item.median_dollar_volume, item.ticker))
    selected = tuple(eligible[: spec.top_n])
    if len(selected) < spec.top_n:
        raise UniverseError(f"only {len(selected)} stocks met liquidity rules; top_n is {spec.top_n}")
    selected_tickers = {item.ticker for item in selected}
    exclusions.extend(
        UniverseExclusion(item.ticker, "outside top_n by median dollar volume")
        for item in eligible[spec.top_n :]
        if item.ticker not in selected_tickers
    )
    return FrozenUniverse(
        name=spec.name,
        kind="liquidity",
        as_of=as_of,
        tickers=tuple(item.ticker for item in selected),
        liquidity=selected,
        exclusions=tuple(sorted(exclusions, key=lambda item: item.ticker)),
    )
