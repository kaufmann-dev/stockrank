from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from stockrank.cli import app
from stockrank.config import load_app_config, write_toml
from stockrank.credentials import CredentialStore
from stockrank.runs import RunStore

runner = CliRunner()


def _project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {
                "massive": {},
                "sec": {},
            },
            "defaults": {"model": "deepseek", "mode": "best-bet", "profile": "medium"},
            "models": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
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


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


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
    assert "system keyring" in output
    assert "secret-value" not in output


def test_adds_multiple_model_profiles_and_stores_keys_outside_toml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stockrank.cli._credentials", lambda: store)

    added = runner.invoke(
        app,
        [
            "model",
            "add",
            "openrouter",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--model",
            "provider/model",
            "--default",
        ],
        input="openrouter-secret\nopenrouter-secret\n",
    )

    assert added.exit_code == 0
    assert "Saved model profile 'openrouter' and made default." in added.stdout
    config = load_app_config(tmp_path)
    assert sorted(config.models) == ["deepseek", "openrouter"]
    assert config.defaults.model == "openrouter"
    assert store.require_llm_key("openrouter") == "openrouter-secret"
    assert "openrouter-secret" not in (tmp_path / "stockrank.toml").read_text()
    assert "API key stored" in runner.invoke(app, ["model", "status"]).stdout


def test_sets_and_clears_keyring_credentials_from_hidden_prompts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stockrank.cli._credentials", lambda: store)

    model_result = runner.invoke(
        app,
        ["model", "set-key", "deepseek"],
        input="llm-secret\nllm-secret\n",
    )
    massive_result = runner.invoke(
        app,
        ["massive", "set-key"],
        input="massive-secret\nmassive-secret\n",
    )

    assert model_result.exit_code == 0
    assert massive_result.exit_code == 0
    assert store.require_llm_key("deepseek") == "llm-secret"
    assert store.require_massive_key() == "massive-secret"
    assert "llm-secret" not in model_result.stdout
    assert "massive-secret" not in massive_result.stdout
    assert "stored" in runner.invoke(app, ["massive", "status"]).stdout
    assert runner.invoke(app, ["massive", "clear-key"]).exit_code == 0
    assert store.get_massive_key() is None


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
