from __future__ import annotations

import csv
import io

from stockrank.ranking import RankingResult, RankingRow, RepresentativeRationale
from stockrank.reporting import ranking_csv, ranking_payload, ranking_report


def _result() -> RankingResult:
    rationale = RepresentativeRationale(
        load_bearing_question="Can margins recover?",
        thesis="Fresh demand evidence outweighs the valuation premium.",
        evidence_ids=("ABC-price-123", "ABC-filing-456"),
        falsifier="Two quarters of falling unit volume.",
        conviction=0.75,
    )
    return RankingResult(
        rows=(
            RankingRow(
                rank=1,
                ticker="ABC",
                strength=0.123456789,
                rank_interval_low=1,
                rank_interval_high=2,
                rank_stddev=0.4,
                appearances=8,
                rationale=rationale,
            ),
        ),
        alpha=0.01,
        bootstrap_samples=1000,
        seed=42,
        min_appearances=8,
    )


def test_ranking_payload_preserves_method_and_rationale() -> None:
    payload = ranking_payload(
        _result(),
        run={"run_id": "run-1"},
        exclusions=({"ticker": "XYZ", "reason": "insufficient bars"},),
    )

    assert payload["method"]["name"] == "regularized-plackett-luce"
    assert payload["ranking"][0]["rationale"]["evidence_ids"] == (
        "ABC-price-123",
        "ABC-filing-456",
    )
    assert payload["exclusions"] == [{"ticker": "XYZ", "reason": "insufficient bars"}]


def test_ranking_csv_is_machine_readable() -> None:
    rows = list(csv.DictReader(io.StringIO(ranking_csv(_result()))))

    assert rows[0]["ticker"] == "ABC"
    assert rows[0]["evidence_ids"] == "ABC-price-123;ABC-filing-456"
    assert rows[0]["strength"] == "0.1234567890"


def test_ranking_report_contains_citations_and_uncertainty_warning() -> None:
    report = ranking_report(
        _result(),
        run={
            "run_id": "run-1",
            "universe_name": "sample",
            "mode_name": "best-bet",
            "model_name": "deepseek",
            "profile": "medium",
            "completed_at": "2026-07-26T12:00:00Z",
        },
    )

    assert "# Stock ranking: run-1" in report
    assert "`ABC-price-123`" in report
    assert "not probabilities that a thesis is true" in report
