from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import choix
import numpy as np

from stockrank.llm import RaceResult, TickerJudgment
from stockrank.races import is_connected

REGULARIZATION_ALPHA = 0.01
DEFAULT_BOOTSTRAP_SAMPLES = 1_000


class RankingError(ValueError):
    """Raised when completed races cannot support a complete ranking."""


@dataclass(frozen=True)
class RepresentativeRationale:
    load_bearing_question: str
    thesis: str
    evidence_ids: tuple[str, ...]
    falsifier: str
    conviction: float


@dataclass(frozen=True)
class RankingRow:
    rank: int
    ticker: str
    strength: float
    rank_interval_low: int
    rank_interval_high: int
    rank_stddev: float
    appearances: int
    rationale: RepresentativeRationale

    @property
    def representative_thesis(self) -> str:
        return self.rationale.thesis

    @property
    def representative_evidence_ids(self) -> tuple[str, ...]:
        return self.rationale.evidence_ids


@dataclass(frozen=True)
class RankingResult:
    rows: tuple[RankingRow, ...]
    alpha: float
    bootstrap_samples: int
    seed: int
    min_appearances: int

    def row_for(self, ticker: str) -> RankingRow:
        for row in self.rows:
            if row.ticker == ticker:
                return row
        raise KeyError(ticker)


def fit_ranking(
    tickers: Sequence[str],
    completed_races: Sequence[RaceResult],
    seed: int,
    *,
    min_appearances: int = 8,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    alpha: float = REGULARIZATION_ALPHA,
) -> RankingResult:
    """Fit a regularized Plackett-Luce ranking and bootstrap rank uncertainty."""

    universe = _validate_inputs(
        tickers,
        completed_races,
        min_appearances=min_appearances,
        bootstrap_samples=bootstrap_samples,
        alpha=alpha,
    )
    races = tuple(completed_races)
    ticker_to_index = {ticker: index for index, ticker in enumerate(universe)}
    ranking_data = sorted(tuple(ticker_to_index[ticker] for ticker in result.ranking) for result in races)

    strengths = np.asarray(
        choix.ilsr_rankings(len(universe), ranking_data, alpha=alpha),
        dtype=float,
    )
    if strengths.shape != (len(universe),) or not np.all(np.isfinite(strengths)):
        raise RankingError("Plackett-Luce fit returned non-finite strengths")

    bootstrap_ranks = _bootstrap_rankings(
        universe,
        ranking_data,
        seed=seed,
        samples=bootstrap_samples,
        alpha=alpha,
    )
    appearances: Counter[str] = Counter(ticker for result in races for ticker in result.ranking)
    rationales = {ticker: _representative_rationale(ticker, races) for ticker in universe}
    ordered_indices = sorted(
        range(len(universe)),
        key=lambda index: (-strengths[index], universe[index]),
    )

    rows: list[RankingRow] = []
    for rank, ticker_index in enumerate(ordered_indices, start=1):
        ticker_ranks = bootstrap_ranks[:, ticker_index]
        lower, upper = np.quantile(ticker_ranks, [0.025, 0.975])
        rows.append(
            RankingRow(
                rank=rank,
                ticker=universe[ticker_index],
                strength=float(strengths[ticker_index]),
                rank_interval_low=max(1, math.floor(lower)),
                rank_interval_high=min(len(universe), math.ceil(upper)),
                rank_stddev=float(np.std(ticker_ranks, ddof=1) if bootstrap_samples > 1 else 0.0),
                appearances=appearances[universe[ticker_index]],
                rationale=rationales[universe[ticker_index]],
            )
        )
    return RankingResult(
        rows=tuple(rows),
        alpha=alpha,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        min_appearances=min_appearances,
    )


def _validate_inputs(
    tickers: Sequence[str],
    completed_races: Sequence[RaceResult],
    *,
    min_appearances: int,
    bootstrap_samples: int,
    alpha: float,
) -> tuple[str, ...]:
    universe = tuple(tickers)
    if len(universe) < 2:
        raise RankingError("ranking requires at least two tickers")
    if any(not isinstance(ticker, str) or not ticker.strip() for ticker in universe):
        raise RankingError("ranking tickers must be non-empty strings")
    if len(universe) != len(set(universe)):
        raise RankingError("ranking tickers must be distinct")
    if min_appearances < 1:
        raise RankingError("min_appearances must be at least 1")
    if bootstrap_samples < 1:
        raise RankingError("bootstrap_samples must be at least 1")
    if not math.isfinite(alpha) or alpha <= 0:
        raise RankingError("alpha must be a positive finite number")

    races = tuple(completed_races)
    if not races:
        raise RankingError("at least one completed race is required")
    universe_set = set(universe)
    appearances: Counter[str] = Counter()
    groups: list[tuple[str, ...]] = []
    for race_index, result in enumerate(races):
        ranking = tuple(result.ranking)
        if len(ranking) < 2:
            raise RankingError(f"race {race_index} contains fewer than two tickers")
        if len(ranking) != len(set(ranking)):
            raise RankingError(f"race {race_index} contains duplicate tickers")
        unknown = sorted(set(ranking) - universe_set)
        if unknown:
            raise RankingError(f"race {race_index} contains unknown tickers: {unknown}")
        judgment_tickers = [judgment.ticker for judgment in result.judgments]
        if (
            len(judgment_tickers) != len(ranking)
            or len(judgment_tickers) != len(set(judgment_tickers))
            or set(judgment_tickers) != set(ranking)
        ):
            raise RankingError(f"race {race_index} must contain exactly one judgment per ranked ticker")
        appearances.update(ranking)
        groups.append(ranking)

    insufficient = {
        ticker: appearances[ticker] for ticker in universe if appearances[ticker] < min_appearances
    }
    if insufficient:
        details = ", ".join(f"{ticker}={count}" for ticker, count in sorted(insufficient.items()))
        raise RankingError(f"tickers below minimum {min_appearances} appearances: {details}")
    if not is_connected(universe, groups):
        raise RankingError("race co-appearance graph is not connected")
    return universe


def _bootstrap_rankings(
    tickers: Sequence[str],
    ranking_data: Sequence[tuple[int, ...]],
    *,
    seed: int,
    samples: int,
    alpha: float,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    race_count = len(ranking_data)
    ranks = np.empty((samples, len(tickers)), dtype=int)
    for sample_index in range(samples):
        selected = rng.integers(0, race_count, size=race_count)
        sample = [ranking_data[int(index)] for index in selected]
        strengths = np.asarray(
            choix.ilsr_rankings(len(tickers), sample, alpha=alpha),
            dtype=float,
        )
        if not np.all(np.isfinite(strengths)):
            raise RankingError(f"bootstrap sample {sample_index} returned non-finite strengths")
        order = sorted(
            range(len(tickers)),
            key=lambda index: (-strengths[index], tickers[index]),
        )
        for rank, ticker_index in enumerate(order, start=1):
            ranks[sample_index, ticker_index] = rank
    return ranks


def _representative_rationale(
    ticker: str,
    races: Sequence[RaceResult],
) -> RepresentativeRationale:
    candidates: list[tuple[tuple[object, ...], TickerJudgment]] = []
    for result in races:
        race_signature = tuple(result.ranking)
        for judgment in result.judgments:
            if judgment.ticker != ticker:
                continue
            key: tuple[object, ...] = (
                -judgment.conviction,
                race_signature,
                judgment.load_bearing_question,
                judgment.thesis,
                tuple(judgment.evidence_ids),
                judgment.falsifier,
            )
            candidates.append((key, judgment))
    if not candidates:
        raise RankingError(f"no rationale is available for {ticker}")
    _, chosen = min(candidates, key=lambda candidate: candidate[0])
    return RepresentativeRationale(
        load_bearing_question=chosen.load_bearing_question,
        thesis=chosen.thesis,
        evidence_ids=tuple(chosen.evidence_ids),
        falsifier=chosen.falsifier,
        conviction=chosen.conviction,
    )
