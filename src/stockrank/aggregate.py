from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalResult:
    ticker: str
    score: float
    confidence: float
    summary: str


@dataclass(frozen=True)
class Position:
    ticker: str
    weight: float
    rationale: str


@dataclass(frozen=True)
class PortfolioDoc:
    summary: str
    positions: list[Position]
    mode: str | None = None
    harness: str | None = None


def parse_toml_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except tomllib.TOMLDecodeError as exc:
        return None, f"invalid TOML: {exc}"


def parse_score_result(path: Path, expected_ticker: str) -> tuple[EvalResult | None, str | None]:
    raw, error = parse_toml_file(path)
    if error:
        return None, error
    assert raw is not None
    if raw.get("ticker") != expected_ticker:
        return None, f"ticker mismatch: expected {expected_ticker}, got {raw.get('ticker')!r}"
    score = raw.get("score")
    confidence = raw.get("confidence")
    summary = raw.get("summary")
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= float(score) <= 100:
        return None, "score must be numeric in range 0..100"
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= float(confidence) <= 1:
        return None, "confidence must be numeric in range 0..1"
    if not isinstance(summary, str) or not summary.strip():
        return None, "summary must be non-empty text"
    return EvalResult(ticker=expected_ticker, score=float(score), confidence=float(confidence), summary=summary.strip()), None


def parse_portfolio_doc(path: Path, *, proposal: bool) -> tuple[PortfolioDoc | None, list[str]]:
    raw, error = parse_toml_file(path)
    if error:
        return None, [error]
    assert raw is not None
    warnings: list[str] = []
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, ["summary must be non-empty text"]
    if proposal:
        if not isinstance(raw.get("mode"), str) or not raw.get("mode"):
            return None, ["proposal mode must be non-empty text"]
        if not isinstance(raw.get("harness"), str) or not raw.get("harness"):
            return None, ["proposal harness must be non-empty text"]
    positions_raw = raw.get("positions")
    if not isinstance(positions_raw, list) or not positions_raw:
        return None, ["positions must be a non-empty array of tables"]
    positions: list[Position] = []
    for index, item in enumerate(positions_raw):
        if not isinstance(item, dict):
            return None, [f"positions[{index}] must be a table"]
        ticker = item.get("ticker")
        weight = item.get("weight")
        rationale = item.get("rationale")
        if not isinstance(ticker, str) or not ticker.strip():
            return None, [f"positions[{index}].ticker must be non-empty text"]
        if isinstance(weight, bool) or not isinstance(weight, int | float) or not math.isfinite(float(weight)) or float(weight) < 0:
            return None, [f"positions[{index}].weight must be a non-negative number"]
        if not isinstance(rationale, str) or not rationale.strip():
            return None, [f"positions[{index}].rationale must be non-empty text"]
        positions.append(Position(ticker=ticker.upper(), weight=float(weight), rationale=rationale.strip()))
    return PortfolioDoc(
        summary=summary.strip(),
        positions=positions,
        mode=raw.get("mode") if proposal else None,
        harness=raw.get("harness") if proposal else None,
    ), warnings


def normalize_positions(
    positions: list[Position],
    universe: set[str],
    *,
    label: str,
) -> tuple[list[Position], list[str]]:
    warnings: list[str] = []
    kept: list[Position] = []
    for position in positions:
        if position.ticker not in universe:
            warnings.append(f"{label}: dropped non-universe ticker {position.ticker}")
            continue
        kept.append(position)
    total = sum(position.weight for position in kept)
    if not kept:
        return [], warnings + [f"{label}: no valid universe positions"]
    if total <= 0:
        return [], warnings + [f"{label}: valid positions have zero total weight"]
    if abs(total - 1.0) > 0.01:
        warnings.append(f"{label}: weights sum to {total:.6f}; renormalized to 1.0")
    return [Position(position.ticker, position.weight / total, position.rationale) for position in kept], warnings
