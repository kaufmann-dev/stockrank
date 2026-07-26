from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from stockrank.cli import app
from stockrank.config import write_toml
from stockrank.runs import RunStore

runner = CliRunner()


def _project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {
                "massive": {"api_key_env": "MASSIVE_API_KEY"},
                "sec": {"user_agent_env": "SEC_USER_AGENT"},
            },
            "defaults": {"model": "deepseek", "mode": "best-bet", "profile": "medium"},
            "models": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                }
            },
        },
    )
    write_toml(
        root / "modes" / "best-bet.toml",
        {
            "name": "best-bet",
            "rank_1_meaning": "best bet",
            "prompt": "Rank the supplied stocks.",
        },
    )
    write_toml(
        root / "universes" / "small.toml",
        {"name": "Small", "kind": "static", "tickers": ["AAA", "BBB"]},
    )


def test_version_and_new_run_requires_explicit_universe() -> None:
    assert runner.invoke(app, ["version"]).stdout.strip() == "1.0.0"
    result = runner.invoke(app, ["rank"])
    assert result.exit_code == 1
    assert "--universe is required" in result.stdout


def test_lists_and_shows_saved_assets_and_redacted_model(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["universe", "list"]).stdout.strip() == "small"
    assert "best-bet" in runner.invoke(app, ["mode", "show", "best-bet"]).stdout

    output = runner.invoke(app, ["model", "show", "deepseek"]).stdout
    assert "https://api.deepseek.com" in output
    assert "DEEPSEEK_API_KEY" in output
    assert "secret-value" not in output


def test_resume_rejects_new_run_options() -> None:
    result = runner.invoke(app, ["rank", "--resume", "123", "--universe", "small"])
    assert result.exit_code == 1
    assert "cannot be combined" in result.stdout


def test_lists_and_shows_v1_run_manifests(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path)
    manifest = RunStore(tmp_path).create(
        universe={"name": "Small", "kind": "static", "as_of": "2026-07-26", "tickers": ["AAA"]},
        mode={"name": "best-bet", "rank_1_meaning": "best bet", "prompt": "Rank."},
        model={"name": "deepseek"},
        settings={"data": {}, "sources": {}},
        profile="medium",
        seed=42,
        now=datetime(2026, 7, 26, 12, tzinfo=UTC),
    )
    monkeypatch.chdir(tmp_path)

    listing = runner.invoke(app, ["runs", "list"])
    assert listing.exit_code == 0
    assert manifest.id in listing.stdout
    assert "running" in listing.stdout
    assert "Small" in listing.stdout

    shown = runner.invoke(app, ["runs", "show", manifest.id])
    assert shown.exit_code == 0
    assert f'"id": "{manifest.id}"' in shown.stdout
    assert '"status": "running"' in shown.stdout
