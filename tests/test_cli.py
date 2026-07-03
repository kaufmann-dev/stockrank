from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from stockrank.cli import app

from conftest import FakeMassiveClient


def test_cli_full_happy_path_with_mocked_massive(mini_project: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(mini_project)
    monkeypatch.setattr("stockrank.prepare.MassiveClient.from_env", lambda root: FakeMassiveClient())
    result = runner.invoke(app, ["prepare", "scores", "--strategy", "mini"])
    assert result.exit_code == 0, result.output
    run_id = sorted((mini_project / "runs").iterdir())[-1].name
    work = mini_project / "runs" / run_id / "scores" / "work" / "quality"
    for harness in ("codex", "claude-code"):
        for ticker, score in (("AAA", 90), ("BBB", 20)):
            (work / harness / ticker / "results.toml").write_text(
                f'ticker = "{ticker}"\nscore = {score}\nconfidence = 0.8\nsummary = "ok"\n',
                encoding="utf-8",
            )
    assert runner.invoke(app, ["finalize", "scores", "--run", run_id]).exit_code == 0
    assert runner.invoke(app, ["prepare", "proposals", "--run", run_id]).exit_code == 0
    proposal = mini_project / "runs" / run_id / "proposals" / "work" / "builder__codex" / "proposal.toml"
    proposal.write_text(
        'mode = "builder"\nharness = "codex"\nsummary = "AAA."\n[[positions]]\nticker = "AAA"\nweight = 1.0\nrationale = "Top."\n',
        encoding="utf-8",
    )
    assert runner.invoke(app, ["finalize", "proposals", "--run", run_id]).exit_code == 0
    assert runner.invoke(app, ["prepare", "portfolio", "--run", run_id]).exit_code == 0
    portfolio = mini_project / "runs" / run_id / "portfolio" / "work" / "portfolio.toml"
    portfolio.write_text(
        'summary = "AAA."\n[[positions]]\nticker = "AAA"\nweight = 1.0\nrationale = "Top."\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["finalize", "portfolio", "--run", run_id])
    assert result.exit_code == 0, result.output
    assert (mini_project / "runs" / run_id / "portfolio" / "portfolio.csv").exists()
