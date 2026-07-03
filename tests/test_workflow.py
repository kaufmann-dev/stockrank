from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from stockrank.finalize import finalize_portfolio, finalize_proposals, finalize_scores
from stockrank.live import commit_run
from stockrank.prepare import prepare_portfolio, prepare_proposals, prepare_scores
from stockrank.tracking import track_run

from conftest import FakeMassiveClient


def _write_scores(root: Path, run_id: str) -> None:
    work = root / "runs" / run_id / "scores" / "work" / "quality"
    (work / "codex" / "AAA" / "results.toml").write_text(
        'ticker = "AAA"\nscore = 80\nconfidence = 0.9\nsummary = "Strong."\n',
        encoding="utf-8",
    )
    (work / "claude-code" / "AAA" / "results.toml").write_text(
        'ticker = "AAA"\nscore = 100\nconfidence = 0.8\nsummary = "Excellent."\n',
        encoding="utf-8",
    )
    (work / "codex" / "BBB" / "results.toml").write_text(
        'ticker = "BBB"\nscore = 10\nconfidence = 0.4\nsummary = "Weak."\n',
        encoding="utf-8",
    )
    (work / "claude-code" / "BBB" / "results.toml").write_text("score = 200\n", encoding="utf-8")


def test_full_prepare_finalize_workflow_and_prompt_workdirs(mini_project: Path) -> None:
    run_id = prepare_scores(mini_project, "mini", client=FakeMassiveClient())
    _write_scores(mini_project, run_id)
    finalize_scores(mini_project, run_id=run_id)
    with (mini_project / "runs" / run_id / "scores" / "scores.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["final_score"] == "95.000000"
    assert rows[1]["ticker"] == "BBB"
    assert rows[1]["quality__score"] == "10.000000"

    prepare_proposals(mini_project, run_id=run_id)
    proposal_task = mini_project / "runs" / run_id / "proposals" / "work" / "builder__codex" / "task.txt"
    assert "Your working folder is `.`" in proposal_task.read_text(encoding="utf-8")
    (proposal_task.parent / "proposal.toml").write_text(
        """
mode = "other"
harness = "codex"
summary = "Prefer AAA."

[[positions]]
ticker = "AAA"
weight = 2.0
rationale = "Best score."

[[positions]]
ticker = "ZZZ"
weight = 1.0
rationale = "Not in universe."
""",
        encoding="utf-8",
    )
    finalize_proposals(mini_project, run_id=run_id)
    with (mini_project / "runs" / run_id / "proposals" / "proposals.csv").open(newline="", encoding="utf-8") as fh:
        proposal_rows = list(csv.DictReader(fh))
    assert proposal_rows == [{"mode": "builder", "harness": "codex", "ticker": "AAA", "weight": "1.000000", "rationale": "Best score."}]
    warnings = (mini_project / "runs" / run_id / "proposals" / "proposals.md").read_text(encoding="utf-8")
    assert "proposal mode field" in warnings
    assert "dropped non-universe ticker ZZZ" in warnings

    prepare_portfolio(mini_project, run_id=run_id)
    portfolio_task = mini_project / "runs" / run_id / "portfolio" / "work" / "task.txt"
    assert "Your working folder is `.`" in portfolio_task.read_text(encoding="utf-8")
    assert (portfolio_task.parent / "current.csv").read_text(encoding="utf-8") == "ticker,weight,rationale\n"
    (portfolio_task.parent / "portfolio.toml").write_text(
        """
summary = "Own AAA and BBB."

[[positions]]
ticker = "AAA"
weight = 0.75
rationale = "Leader."

[[positions]]
ticker = "BBB"
weight = 0.25
rationale = "Diversifier."
""",
        encoding="utf-8",
    )
    finalize_portfolio(mini_project, run_id=run_id, now=datetime(2026, 7, 4, tzinfo=UTC))
    assert "inception = \"2026-07-06\"" in (mini_project / "runs" / run_id / "run.toml").read_text(encoding="utf-8")
    with (mini_project / "runs" / run_id / "portfolio" / "trades.csv").open(newline="", encoding="utf-8") as fh:
        trades = list(csv.DictReader(fh))
    assert trades[0] == {"ticker": "AAA", "current_weight": "0.000000", "target_weight": "0.750000", "delta": "0.750000"}


def test_commit_snapshots_prior_live_and_renders_current(mini_project: Path) -> None:
    first = prepare_scores(mini_project, "mini", client=FakeMassiveClient())
    _write_scores(mini_project, first)
    finalize_scores(mini_project, run_id=first)
    prepare_proposals(mini_project, run_id=first)
    (mini_project / "runs" / first / "proposals" / "work" / "builder__codex" / "proposal.toml").write_text(
        'mode = "builder"\nharness = "codex"\nsummary = "AAA."\n[[positions]]\nticker = "AAA"\nweight = 1.0\nrationale = "Best."\n',
        encoding="utf-8",
    )
    finalize_proposals(mini_project, run_id=first)
    prepare_portfolio(mini_project, run_id=first)
    (mini_project / "runs" / first / "portfolio" / "work" / "portfolio.toml").write_text(
        'summary = "AAA."\n[[positions]]\nticker = "AAA"\nweight = 1.0\nrationale = "Best."\n',
        encoding="utf-8",
    )
    finalize_portfolio(mini_project, run_id=first)
    commit_run(mini_project, run_id=first, confirm=True, now=datetime(2026, 7, 6, tzinfo=UTC))
    assert 'run_id = "' + first + '"' in (mini_project / "live" / "portfolio.toml").read_text(encoding="utf-8")

    second = prepare_scores(mini_project, "mini", client=FakeMassiveClient())
    _write_scores(mini_project, second)
    finalize_scores(mini_project, run_id=second)
    prepare_proposals(mini_project, run_id=second)
    (mini_project / "runs" / second / "proposals" / "work" / "builder__codex" / "proposal.toml").write_text(
        'mode = "builder"\nharness = "codex"\nsummary = "BBB."\n[[positions]]\nticker = "BBB"\nweight = 1.0\nrationale = "Turn."\n',
        encoding="utf-8",
    )
    finalize_proposals(mini_project, run_id=second)
    prepare_portfolio(mini_project, run_id=second)
    current_md = (mini_project / "runs" / second / "portfolio" / "work" / "current.md").read_text(encoding="utf-8")
    assert "Strategy: mini" in current_md
    (mini_project / "runs" / second / "portfolio" / "work" / "portfolio.toml").write_text(
        'summary = "BBB."\n[[positions]]\nticker = "BBB"\nweight = 1.0\nrationale = "Turn."\n',
        encoding="utf-8",
    )
    finalize_portfolio(mini_project, run_id=second)
    commit_run(mini_project, run_id=second, confirm=True, now=datetime(2026, 7, 7, tzinfo=UTC))
    assert list((mini_project / "live" / "history").glob("*.toml"))


def test_tracking_math_uses_buy_and_hold_fixed_weights(mini_project: Path) -> None:
    run_id = prepare_scores(mini_project, "mini", client=FakeMassiveClient())
    _write_scores(mini_project, run_id)
    finalize_scores(mini_project, run_id=run_id)
    prepare_proposals(mini_project, run_id=run_id)
    (mini_project / "runs" / run_id / "proposals" / "work" / "builder__codex" / "proposal.toml").write_text(
        'mode = "builder"\nharness = "codex"\nsummary = "AAA."\n[[positions]]\nticker = "AAA"\nweight = 1.0\nrationale = "Best."\n',
        encoding="utf-8",
    )
    finalize_proposals(mini_project, run_id=run_id)
    prepare_portfolio(mini_project, run_id=run_id)
    (mini_project / "runs" / run_id / "portfolio" / "work" / "portfolio.toml").write_text(
        'summary = "Mix."\n[[positions]]\nticker = "AAA"\nweight = 0.5\nrationale = "Half."\n[[positions]]\nticker = "BBB"\nweight = 0.5\nrationale = "Half."\n',
        encoding="utf-8",
    )
    finalize_portfolio(mini_project, run_id=run_id, now=datetime(2026, 1, 2, tzinfo=UTC))
    closes = {
        "AAA": {date(2026, 1, 2): 10.0, date(2026, 1, 5): 20.0},
        "BBB": {date(2026, 1, 2): 20.0, date(2026, 1, 5): 10.0},
        "SPY": {date(2026, 1, 2): 100.0, date(2026, 1, 5): 110.0},
    }
    track_run(mini_project, run_id=run_id, client=FakeMassiveClient(closes), today=date(2026, 1, 5))
    with (mini_project / "runs" / run_id / "portfolio" / "performance.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[-1]["portfolio"] == "1.250000"
    assert rows[-1]["benchmark"] == "1.100000"
