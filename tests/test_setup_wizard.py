from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from prompt_toolkit.styles import Style
from rich.console import Console

from stockrank.config import ModelConfig, initialize_project, load_app_config, user_config_path, write_toml
from stockrank.credentials import CredentialStore
from stockrank.model_catalog import CatalogError, CatalogModel, CatalogProvider
from stockrank.setup_wizard import (
    SetupCancelled,
    SetupPrompts,
    TerminalSetupPrompts,
    configure_setup,
    save_model_profile_with_key,
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


class ScriptedPrompts(SetupPrompts):
    def __init__(
        self,
        *,
        autocomplete: Sequence[str] = (),
        text: Sequence[str] = (),
        confirm: Sequence[bool] = (),
        secret: Sequence[str] = (),
    ) -> None:
        self.autocomplete_answers = list(autocomplete)
        self.text_answers = list(text)
        self.confirm_answers = list(confirm)
        self.secret_answers = list(secret)
        self.notices: list[str] = []

    def autocomplete(self, message: str, choices: Sequence[str]) -> str:
        del message
        answer = self.autocomplete_answers.pop(0)
        assert answer in choices
        return answer

    def text(self, message: str, *, default: str = "", validate=None) -> str:
        del message, default
        answer = self.text_answers.pop(0)
        if validate is not None:
            assert validate(answer) is True
        return answer

    def confirm(self, message: str, *, default: bool = False) -> bool:
        del message, default
        return self.confirm_answers.pop(0)

    def secret(self, label: str) -> str:
        del label
        return self.secret_answers.pop(0)

    def notice(self, message: str) -> None:
        self.notices.append(message)


class StaticCatalog:
    def __init__(self, *providers: CatalogProvider) -> None:
        self.values = tuple(providers)
        self.calls = 0

    def providers(self) -> tuple[CatalogProvider, ...]:
        self.calls += 1
        return self.values


class FailingCatalog:
    def providers(self) -> tuple[CatalogProvider, ...]:
        raise CatalogError("offline")


DEEPSEEK = CatalogProvider(
    id="deepseek",
    name="DeepSeek",
    base_url="https://api.deepseek.com",
    models=(CatalogModel(id="deepseek-v4-pro", name="DeepSeek V4 Pro"),),
)
OPENROUTER = CatalogProvider(
    id="openrouter",
    name="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    models=(CatalogModel(id="provider/model", name="Provider Model"),),
)


def _configured_project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {"massive": {}, "sec": {}},
            "defaults": {},
        },
    )
    write_toml(
        user_config_path(),
        {
            "defaults": {"model": "deepseek"},
            "models": {
                "deepseek": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-pro",
                }
            },
        },
    )


def test_terminal_autocomplete_uses_dark_high_contrast_menu(monkeypatch) -> None:
    captured: dict[str, object] = {}
    question = object()

    def autocomplete(message: str, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return question

    monkeypatch.setattr("stockrank.setup_wizard.questionary.autocomplete", autocomplete)
    monkeypatch.setattr("stockrank.setup_wizard._unsafe_ask", lambda value: "Alpha")

    selected = TerminalSetupPrompts(Console()).autocomplete("Provider", ["Alpha", "Beta"])

    assert selected == "Alpha"
    assert captured["message"] == "Provider"
    assert captured["choices"] == ["Alpha", "Beta"]
    style = captured["style"]
    assert isinstance(style, Style)
    rules = dict(style.style_rules)
    assert rules["completion-menu"] == "fg:#e5e7eb bg:#1f2937"
    assert rules["completion-menu.completion"] == "fg:#e5e7eb bg:#1f2937 nobold"
    assert rules["completion-menu.completion.current"] == (
        "fg:#ffffff bg:#2563eb noreverse"
    )
    assert rules["answer"] == "fg:#e5e7eb"
    assert rules["selected"] == "fg:#ffffff bg:#2563eb bold noreverse"


def test_fresh_setup_configures_model_and_both_keyring_secrets(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    prompts = ScriptedPrompts(
        autocomplete=["DeepSeek (deepseek)", "DeepSeek V4 Pro (deepseek-v4-pro)"],
        text=["deepseek"],
        secret=["llm-secret", "massive-secret"],
    )

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=StaticCatalog(DEEPSEEK),
    )

    config = load_app_config(tmp_path)
    assert config.defaults.model == "deepseek"
    assert config.models["deepseek"].provider == "deepseek"
    assert config.models["deepseek"].model == "deepseek-v4-pro"
    assert store.require_llm_key("deepseek") == "llm-secret"
    assert store.require_massive_key() == "massive-secret"
    rendered = (tmp_path / "stockrank.toml").read_text()
    output = "\n".join(prompts.notices)
    assert "llm-secret" not in rendered + output
    assert "massive-secret" not in rendered + output


def test_existing_setup_can_add_and_default_another_model(tmp_path: Path) -> None:
    _configured_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    store.set_massive_key("existing-massive")
    prompts = ScriptedPrompts(
        autocomplete=["OpenRouter (openrouter)", "Provider Model (provider/model)"],
        text=["openrouter"],
        confirm=[True, True],
        secret=["router-secret"],
    )

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=StaticCatalog(OPENROUTER),
    )

    config = load_app_config(tmp_path)
    assert config.defaults.model == "openrouter"
    assert sorted(config.models) == ["deepseek", "openrouter"]
    assert store.require_llm_key("openrouter") == "router-secret"
    assert store.require_massive_key() == "existing-massive"
    assert "Massive API key already stored." in prompts.notices


def test_declining_another_model_still_configures_missing_massive_key(tmp_path: Path) -> None:
    _configured_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    catalog = StaticCatalog()
    prompts = ScriptedPrompts(confirm=[False], secret=["massive-secret"])

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=catalog,
    )

    assert catalog.calls == 0
    assert list(load_app_config(tmp_path).models) == ["deepseek"]
    assert store.require_massive_key() == "massive-secret"


def test_catalog_failure_falls_back_to_custom_provider(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    prompts = ScriptedPrompts(
        text=["custom", "https://models.example.com/v1", "custom-model", "custom"],
        secret=["custom-secret", "massive-secret"],
    )

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=FailingCatalog(),
    )

    profile = load_app_config(tmp_path).models["custom"]
    assert profile.provider == "custom"
    assert profile.base_url == "https://models.example.com/v1"
    assert profile.model == "custom-model"
    assert any("continuing with a custom provider" in notice for notice in prompts.notices)


def test_duplicate_profile_name_is_reprompted(tmp_path: Path) -> None:
    _configured_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    store.set_massive_key("massive")
    prompts = ScriptedPrompts(
        autocomplete=["DeepSeek (deepseek)", "DeepSeek V4 Pro (deepseek-v4-pro)"],
        text=["deepseek", "deepseek-alt"],
        confirm=[True, False],
        secret=["second-key"],
    )

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=StaticCatalog(DEEPSEEK),
    )

    assert "deepseek-alt" in load_app_config(tmp_path).models
    assert any("already exists" in notice for notice in prompts.notices)


def test_invalid_custom_provider_values_are_reprompted(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    prompts = ScriptedPrompts(
        text=[
            "bad provider",
            "not-a-url",
            "model",
            "custom",
            "https://models.example.com/v1",
            "custom-model",
            "custom",
        ],
        secret=["custom-secret", "massive-secret"],
    )

    configure_setup(
        tmp_path,
        credentials=store,
        prompts=prompts,
        catalog=FailingCatalog(),
    )

    assert "custom" in load_app_config(tmp_path).models
    assert any("Invalid provider configuration" in notice for notice in prompts.notices)


def test_cancellation_leaves_resumable_zero_model_project(tmp_path: Path) -> None:
    initialize_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    prompts = ScriptedPrompts(
        autocomplete=["DeepSeek (deepseek)", "DeepSeek V4 Pro (deepseek-v4-pro)"],
        text=["deepseek"],
    )

    def cancel(_label: str) -> str:
        raise SetupCancelled("setup cancelled")

    prompts.secret = cancel  # type: ignore[method-assign]
    with pytest.raises(SetupCancelled, match="setup cancelled"):
        configure_setup(
            tmp_path,
            credentials=store,
            prompts=prompts,
            catalog=StaticCatalog(DEEPSEEK),
        )

    assert load_app_config(tmp_path).models == {}
    assert store.get_llm_key("deepseek") is None
    assert store.get_massive_key() is None


def test_profile_save_restores_previous_key_when_config_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _configured_project(tmp_path)
    store = CredentialStore(MemoryKeyring())
    store.set_llm_key("deepseek", "previous")
    profile = ModelConfig(
        name="deepseek",
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "stockrank.setup_wizard.save_model_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )

    with pytest.raises(RuntimeError, match="write failed"):
        save_model_profile_with_key(
            tmp_path,
            profile,
            "replacement",
            store,
            replace_existing=True,
        )

    assert store.require_llm_key("deepseek") == "previous"
