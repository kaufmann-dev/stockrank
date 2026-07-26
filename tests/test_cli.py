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
                    "provider": "deepseek",
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


def test_model_list_reports_uninitialized_project_without_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["model", "list"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "Not initialized. Run 'stockrank init'."
    assert list(tmp_path.iterdir()) == []
    add_result = runner.invoke(
        app,
        [
            "model",
            "add",
            "example",
            "--provider",
            "example",
            "--base-url",
            "https://example.com/v1",
            "--model",
            "example",
        ],
    )
    assert add_result.exit_code == 1
    assert "project is not initialized" in add_result.stdout
    assert "API key" not in add_result.stdout


def test_initializes_complete_zero_model_project(tmp_path: Path, monkeypatch) -> None:
    setup_roots: list[Path] = []
    monkeypatch.setattr("stockrank.cli._run_setup", setup_roots.append)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "Initialized stockrank project" in result.stdout
    assert "Setup complete." in result.stdout
    assert setup_roots == [tmp_path]
    assert (tmp_path / "stockrank.toml").is_file()
    assert (tmp_path / "modes" / "best-bet.toml").is_file()
    assert (tmp_path / "universes" / "liquid-50.toml").is_file()
    config = load_app_config(tmp_path)
    assert config.models == {}
    assert config.defaults.model is None
    assert runner.invoke(app, ["model", "list"]).stdout.strip() == "(none)"
    assert runner.invoke(app, ["mode", "list"]).stdout.strip() == "best-bet"
    assert runner.invoke(app, ["universe", "list"]).stdout.strip() == "liquid-50"
    config_before = (tmp_path / "stockrank.toml").read_text()
    repeated = runner.invoke(app, ["init"])
    assert repeated.exit_code == 0
    assert f"Already initialized in {tmp_path}." in repeated.stdout
    assert setup_roots == [tmp_path, tmp_path]
    assert (tmp_path / "stockrank.toml").read_text() == config_before


def test_init_preserves_valid_partial_assets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("stockrank.cli._run_setup", lambda root: None)
    existing_mode = tmp_path / "modes" / "best-bet.toml"
    write_toml(
        existing_mode,
        {
            "name": "best-bet",
            "rank_1_meaning": "custom winner",
            "prompt": "Use the custom ranking objective.",
        },
    )
    existing_universe = tmp_path / "universes" / "liquid-50.toml"
    write_toml(
        existing_universe,
        {
            "name": "Custom liquid universe",
            "kind": "liquidity",
            "top_n": 25,
            "lookback_sessions": 10,
            "min_price": 10.0,
            "exchanges": ["XNYS"],
        },
    )
    mode_bytes = existing_mode.read_bytes()
    universe_bytes = existing_universe.read_bytes()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "Created stockrank.toml." in result.stdout
    assert "Preserved modes/best-bet.toml, universes/liquid-50.toml." in result.stdout
    assert existing_mode.read_bytes() == mode_bytes
    assert existing_universe.read_bytes() == universe_bytes
    assert (tmp_path / "stockrank.toml").is_file()


def test_init_refuses_invalid_partial_assets_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conflicting = tmp_path / "modes" / "best-bet.toml"
    conflicting.parent.mkdir()
    conflicting.write_text("existing")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "existing modes/best-bet.toml is invalid" in result.stdout
    assert conflicting.read_text() == "existing"
    assert not (tmp_path / "stockrank.toml").exists()
    assert not (tmp_path / "universes" / "liquid-50.toml").exists()


def test_init_refuses_non_file_starter_target_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conflicting = tmp_path / "universes" / "liquid-50.toml"
    conflicting.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "existing universes/liquid-50.toml is not a file" in result.stdout
    assert conflicting.is_dir()
    assert not (tmp_path / "stockrank.toml").exists()
    assert not (tmp_path / "modes").exists()


def test_init_refuses_invalid_existing_config_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "stockrank.toml"
    config_path.write_text("[invalid")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "existing stockrank.toml is invalid" in result.stdout
    assert config_path.read_text() == "[invalid"
    assert not (tmp_path / "modes").exists()
    assert not (tmp_path / "universes").exists()


def test_init_preserves_valid_customized_project(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path)
    monkeypatch.setattr("stockrank.cli._run_setup", lambda root: None)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert f"Already initialized in {tmp_path}." in result.stdout
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_lists_and_shows_saved_assets_and_redacted_model(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["universe", "list"]).stdout.strip() == "small"
    assert "best-bet" in runner.invoke(app, ["mode", "show", "best-bet"]).stdout

    output = runner.invoke(app, ["model", "show", "deepseek"]).stdout
    assert '"provider": "deepseek"' in output
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
            "--provider",
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
    assert config.models["openrouter"].provider == "openrouter"
    assert config.defaults.model == "openrouter"
    assert store.require_llm_key("openrouter") == "openrouter-secret"
    assert "openrouter-secret" not in (tmp_path / "stockrank.toml").read_text()

    second = runner.invoke(
        app,
        [
            "model",
            "add",
            "example",
            "--provider",
            "example",
            "--base-url",
            "https://example.com/v1",
            "--model",
            "example",
        ],
        input="example-secret\nexample-secret\n",
    )
    assert second.exit_code == 0
    assert "made default" not in second.stdout
    assert load_app_config(tmp_path).defaults.model == "openrouter"
    assert "API key stored" in runner.invoke(app, ["model", "status"]).stdout


def test_first_added_model_becomes_default(tmp_path: Path, monkeypatch) -> None:
    store = CredentialStore(MemoryKeyring())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stockrank.cli._credentials", lambda: store)
    monkeypatch.setattr("stockrank.cli._run_setup", lambda root: None)
    assert runner.invoke(app, ["init"]).exit_code == 0

    added = runner.invoke(
        app,
        [
            "model",
            "add",
            "openrouter",
            "--provider",
            "openrouter",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--model",
            "provider/model",
        ],
        input="openrouter-secret\nopenrouter-secret\n",
    )

    assert added.exit_code == 0
    assert "Saved model profile 'openrouter' and made default." in added.stdout
    config = load_app_config(tmp_path)
    assert config.defaults.model == "openrouter"
    assert store.require_llm_key("openrouter") == "openrouter-secret"
    assert "openrouter-secret" not in (tmp_path / "stockrank.toml").read_text()


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
