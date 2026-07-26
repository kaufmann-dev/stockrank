from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from .ranking import RankingResult


def ranking_payload(
    result: RankingResult,
    *,
    run: Mapping[str, Any],
    exclusions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the canonical machine-readable result without mutating the fit."""

    return {
        "run": dict(run),
        "method": {
            "name": "regularized-plackett-luce",
            "alpha": result.alpha,
            "bootstrap_samples": result.bootstrap_samples,
            "seed": result.seed,
            "min_appearances": result.min_appearances,
        },
        "ranking": [asdict(row) for row in result.rows],
        "exclusions": [dict(exclusion) for exclusion in exclusions],
    }


def ranking_csv(result: RankingResult) -> str:
    """Render the compact analysis artifact."""

    output = io.StringIO(newline="")
    fieldnames = [
        "rank",
        "ticker",
        "strength",
        "rank_interval_low",
        "rank_interval_high",
        "rank_stddev",
        "appearances",
        "conviction",
        "evidence_ids",
        "load_bearing_question",
        "thesis",
        "falsifier",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in result.rows:
        writer.writerow(
            {
                "rank": row.rank,
                "ticker": row.ticker,
                "strength": f"{row.strength:.10f}",
                "rank_interval_low": row.rank_interval_low,
                "rank_interval_high": row.rank_interval_high,
                "rank_stddev": f"{row.rank_stddev:.6f}",
                "appearances": row.appearances,
                "conviction": f"{row.rationale.conviction:.3f}",
                "evidence_ids": ";".join(row.rationale.evidence_ids),
                "load_bearing_question": row.rationale.load_bearing_question,
                "thesis": row.rationale.thesis,
                "falsifier": row.rationale.falsifier,
            }
        )
    return output.getvalue()


def ranking_report(
    result: RankingResult,
    *,
    run: Mapping[str, Any],
    exclusions: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render a readable audit digest without hiding uncertainty or exclusions."""

    run_id = _text(run.get("run_id", "unknown"))
    universe = _text(run.get("universe_name", "unknown"))
    mode = _text(run.get("mode_name", "unknown"))
    model = _text(run.get("model_name", "unknown"))
    profile = _text(run.get("profile", "unknown"))
    completed_at = _text(run.get("completed_at", "unknown"))
    lines = [
        f"# Stock ranking: {run_id}",
        "",
        f"- Universe: {universe}",
        f"- Objective: {mode}",
        f"- Model profile: {model}",
        f"- Race profile: {profile}",
        f"- Completed: {completed_at}",
        (
            f"- Method: regularized Plackett–Luce (alpha {result.alpha:g}) with "
            f"{result.bootstrap_samples:,} seeded bootstrap samples"
        ),
        "",
        "## Ranking",
        "",
    ]
    for row in result.rows:
        evidence = ", ".join(f"`{_inline(item)}`" for item in row.rationale.evidence_ids)
        lines.extend(
            [
                (
                    f"### {row.rank}. {row.ticker} — strength {row.strength:.4f}, "
                    f"95% rank interval {row.rank_interval_low}–{row.rank_interval_high}"
                ),
                "",
                f"Question: {_text(row.rationale.load_bearing_question)}",
                "",
                f"Thesis: {_text(row.rationale.thesis)}",
                "",
                f"Falsifier: {_text(row.rationale.falsifier)}",
                "",
                (
                    f"Evidence: {evidence}. Conviction: {row.rationale.conviction:.2f}. "
                    f"Appearances: {row.appearances}."
                ),
                "",
            ]
        )
    lines.extend(["## Exclusions", ""])
    if exclusions:
        for exclusion in exclusions:
            ticker = _text(exclusion.get("ticker", "unknown"))
            reason = _text(exclusion.get("reason", "unspecified"))
            lines.append(f"- {ticker}: {reason}")
    else:
        lines.append("No candidates were excluded.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "The fitted strength determines the primary order. Bootstrap rank intervals describe "
                "sensitivity to the observed races; they are not probabilities that a thesis is true. "
                "This report is research output, not investment advice."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _text(value: object) -> str:
    return " ".join(str(value).split())


def _inline(value: object) -> str:
    return _text(value).replace("`", "")
