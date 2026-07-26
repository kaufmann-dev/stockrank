from __future__ import annotations

import json

import numpy as np
import pytest

from stockrank.llm import RaceResult
from stockrank.ranking import RankingError, fit_ranking


def result(
    ranking: tuple[str, ...],
    *,
    conviction: dict[str, float] | None = None,
    thesis_suffix: str = "",
) -> RaceResult:
    convictions = conviction or {}
    return RaceResult.model_validate_json(
        json.dumps(
            {
                "ranking": list(ranking),
                "judgments": [
                    {
                        "ticker": ticker,
                        "load_bearing_question": f"Question {ticker}{thesis_suffix}",
                        "thesis": f"Thesis {ticker}{thesis_suffix}",
                        "evidence_ids": [f"{ticker}-evidence"],
                        "falsifier": f"Falsifier {ticker}{thesis_suffix}",
                        "conviction": convictions.get(ticker, 0.5),
                    }
                    for ticker in ranking
                ],
            }
        ),
        strict=True,
    )


def sample_pl_rankings(
    strengths: np.ndarray,
    count: int,
    seed: int,
) -> list[tuple[int, ...]]:
    rng = np.random.default_rng(seed)
    rankings: list[tuple[int, ...]] = []
    for _ in range(count):
        remaining = list(range(len(strengths)))
        ranking: list[int] = []
        while remaining:
            weights = np.exp(strengths[remaining] - np.max(strengths[remaining]))
            position = int(rng.choice(len(remaining), p=weights / weights.sum()))
            ranking.append(remaining.pop(position))
        rankings.append(tuple(ranking))
    return rankings


def test_fit_recovers_synthetic_pl_order_and_bootstraps_uncertainty() -> None:
    tickers = ("A", "B", "C", "D", "E")
    true_strengths = np.array([2.0, 1.0, 0.0, -1.0, -2.0])
    races = [
        result(tuple(tickers[index] for index in ranking))
        for ranking in sample_pl_rankings(true_strengths, count=250, seed=44)
    ]

    fitted = fit_ranking(
        tickers,
        races,
        seed=81,
        min_appearances=250,
        bootstrap_samples=25,
    )

    assert [row.ticker for row in fitted.rows] == list(tickers)
    assert fitted.alpha == 0.01
    assert fitted.bootstrap_samples == 25
    assert all(1 <= row.rank_interval_low <= row.rank_interval_high <= 5 for row in fitted.rows)
    assert all(row.rank_stddev >= 0.0 for row in fitted.rows)
    assert all(row.appearances == 250 for row in fitted.rows)


def test_bootstrap_and_representative_rationale_are_deterministic() -> None:
    tickers = ("A", "B", "C")
    races = [
        result(
            ("A", "B", "C"),
            conviction={"A": 0.4, "B": 0.4, "C": 0.4},
            thesis_suffix="-low",
        ),
        result(
            ("A", "C", "B"),
            conviction={"A": 0.9, "B": 0.6, "C": 0.6},
            thesis_suffix="-high",
        ),
        result(("B", "A", "C")),
    ]

    first = fit_ranking(
        tickers,
        races,
        seed=4,
        min_appearances=3,
        bootstrap_samples=20,
    )
    second = fit_ranking(
        tickers,
        list(reversed(races)),
        seed=4,
        min_appearances=3,
        bootstrap_samples=20,
    )

    assert first.rows == second.rows
    assert first.row_for("A").rationale.thesis == "Thesis A-high"
    assert first.row_for("A").representative_evidence_ids == ("A-evidence",)


def test_minimum_appearance_validation_names_short_tickers() -> None:
    races = [result(("A", "B", "C"))]

    with pytest.raises(RankingError, match=r"A=1.*B=1.*C=1"):
        fit_ranking(
            ("A", "B", "C"),
            races,
            seed=1,
            min_appearances=2,
            bootstrap_samples=2,
        )


def test_disconnected_races_are_rejected_before_fitting() -> None:
    races = [result(("A", "B")), result(("C", "D"))]

    with pytest.raises(RankingError, match="not connected"):
        fit_ranking(
            ("A", "B", "C", "D"),
            races,
            seed=1,
            min_appearances=1,
            bootstrap_samples=2,
        )


def test_unknown_race_ticker_is_rejected() -> None:
    with pytest.raises(RankingError, match="unknown tickers"):
        fit_ranking(
            ("A", "B"),
            [result(("A", "C"))],
            seed=1,
            min_appearances=1,
            bootstrap_samples=2,
        )
