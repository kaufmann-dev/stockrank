from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import questionary
from rich.console import Console

from .config import (
    ConfigError,
    ModelConfig,
    load_app_config,
    save_model_config,
    validate_model_profile,
)
from .credentials import CredentialStore
from .model_catalog import CatalogError, CatalogModel, CatalogProvider, ModelsDevClient

_CUSTOM_PROVIDER = "Custom OpenAI-compatible provider"
_AUTOCOMPLETE_STYLE = questionary.Style(
    [
        ("answer", "fg:#e5e7eb"),
        ("selected", "fg:#ffffff bg:#2563eb bold noreverse"),
        ("completion-menu", "fg:#e5e7eb bg:#1f2937"),
        ("completion-menu.completion", "fg:#e5e7eb bg:#1f2937 nobold"),
        ("completion-menu.completion.current", "fg:#ffffff bg:#2563eb noreverse"),
    ]
)
_T = TypeVar("_T")
Validator = Callable[[str], bool | str]


class SetupPrompts(Protocol):
    def autocomplete(self, message: str, choices: Sequence[str]) -> str: ...

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
    ) -> str: ...

    def confirm(self, message: str, *, default: bool = False) -> bool: ...

    def secret(self, label: str) -> str: ...

    def notice(self, message: str) -> None: ...


class CatalogSource(Protocol):
    def providers(self) -> tuple[CatalogProvider, ...]: ...


class SetupCancelled(ConfigError):
    pass


class TerminalSetupPrompts:
    def __init__(self, console: Console) -> None:
        self._console = console

    def autocomplete(self, message: str, choices: Sequence[str]) -> str:
        allowed = set(choices)
        return _require_answer(
            _unsafe_ask(
                questionary.autocomplete(
                    message,
                    choices=list(choices),
                    ignore_case=True,
                    match_middle=True,
                    style=_AUTOCOMPLETE_STYLE,
                    validate=lambda value: (
                        True if value in allowed else "Select one of the catalog choices."
                    ),
                )
            )
        )

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
    ) -> str:
        return _require_answer(
            _unsafe_ask(questionary.text(message, default=default, validate=validate))
        )

    def confirm(self, message: str, *, default: bool = False) -> bool:
        return bool(_require_answer(_unsafe_ask(questionary.confirm(message, default=default))))

    def secret(self, label: str) -> str:
        nonempty = lambda value: True if value.strip() else f"{label} must not be empty."
        while True:
            value = _require_answer(
                _unsafe_ask(questionary.password(label, validate=nonempty))
            )
            confirmed = _require_answer(
                _unsafe_ask(questionary.password(f"Confirm {label}", validate=nonempty))
            )
            if value == confirmed:
                return value
            self.notice("Values did not match; try again.")

    def notice(self, message: str) -> None:
        self._console.print(message)


def configure_setup(
    root: Path,
    *,
    credentials: CredentialStore,
    prompts: SetupPrompts,
    catalog: CatalogSource | None = None,
) -> None:
    config = load_app_config(root)
    should_add_model = not config.models
    if config.models:
        should_add_model = prompts.confirm("Add another model profile?", default=False)
    if should_add_model:
        profile = _prompt_model_profile(
            prompts,
            catalog or ModelsDevClient(),
            existing_names=set(config.models),
        )
        make_default = bool(config.models) and prompts.confirm(
            f"Make {profile.name!r} the default model profile?",
            default=False,
        )
        made_default = save_model_profile_with_key(
            root,
            profile,
            prompts.secret(f"API key for {profile.provider}"),
            credentials,
            make_default=make_default,
        )
        suffix = " and made default" if made_default else ""
        prompts.notice(f"Saved model profile {profile.name!r}{suffix}.")

    if credentials.get_massive_key() is None:
        credentials.set_massive_key(prompts.secret("Massive API key"))
        prompts.notice("Stored Massive API key.")
    else:
        prompts.notice("Massive API key already stored.")


def save_model_profile_with_key(
    root: Path,
    profile: ModelConfig,
    api_key: str,
    credentials: CredentialStore,
    *,
    make_default: bool = False,
    replace_existing: bool = False,
) -> bool:
    validate_model_profile(profile)
    previous_key = credentials.get_llm_key(profile.name)
    credentials.set_llm_key(profile.name, api_key)
    try:
        return save_model_config(
            root,
            profile,
            make_default=make_default,
            replace=replace_existing,
        )
    except Exception:
        if previous_key is None:
            credentials.delete_llm_key(profile.name)
        else:
            credentials.set_llm_key(profile.name, previous_key)
        raise


def _prompt_model_profile(
    prompts: SetupPrompts,
    catalog: CatalogSource,
    *,
    existing_names: set[str],
) -> ModelConfig:
    try:
        providers = catalog.providers()
    except CatalogError as exc:
        prompts.notice(f"[yellow]warning:[/] {exc}; continuing with a custom provider.")
        profile = _prompt_custom_profile(prompts)
    else:
        profile = _prompt_catalog_profile(prompts, providers)
    return _prompt_profile_name(prompts, profile, existing_names)


def _prompt_catalog_profile(
    prompts: SetupPrompts,
    providers: tuple[CatalogProvider, ...],
) -> ModelConfig:
    labels = {_provider_label(provider): provider for provider in providers}
    selected = prompts.autocomplete("Provider", [*labels, _CUSTOM_PROVIDER])
    if selected == _CUSTOM_PROVIDER:
        return _prompt_custom_profile(prompts)
    provider = labels[selected]
    model_labels = {_model_label(model): model for model in provider.models}
    model = model_labels[prompts.autocomplete("Model", list(model_labels))]
    return ModelConfig(
        name=provider.id,
        provider=provider.id,
        base_url=provider.base_url,
        model=model.id,
    )


def _prompt_custom_profile(prompts: SetupPrompts) -> ModelConfig:
    while True:
        provider = prompts.text("Provider ID", validate=_required)
        base_url = prompts.text("OpenAI-compatible base URL", validate=_required)
        model = prompts.text("Model ID", validate=_required)
        profile = ModelConfig(
            name=provider,
            provider=provider,
            base_url=base_url,
            model=model,
        )
        try:
            validate_model_profile(profile)
        except ConfigError as exc:
            prompts.notice(f"[red]Invalid provider configuration:[/] {exc}")
            continue
        return profile


def _prompt_profile_name(
    prompts: SetupPrompts,
    profile: ModelConfig,
    existing_names: set[str],
) -> ModelConfig:
    default = profile.provider
    if default in existing_names:
        default = _profile_name(profile.provider, profile.model)
    while True:
        name = prompts.text("Profile name", default=default, validate=_required)
        candidate = replace(profile, name=name)
        try:
            validate_model_profile(candidate)
        except ConfigError as exc:
            prompts.notice(f"[red]Invalid profile:[/] {exc}")
            continue
        if candidate.name in existing_names:
            prompts.notice(f"[red]Profile {candidate.name!r} already exists.[/]")
            continue
        return candidate


def _profile_name(provider: str, model: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{provider}-{model}").strip("._-")
    return value or "model"


def _provider_label(provider: CatalogProvider) -> str:
    return f"{provider.name} ({provider.id})"


def _model_label(model: CatalogModel) -> str:
    suffix = f" [{model.status}]" if model.status else ""
    return f"{model.name} ({model.id}){suffix}"


def _required(value: str) -> bool | str:
    return True if value.strip() else "A value is required."


def _unsafe_ask(question: Any) -> Any:
    try:
        return question.unsafe_ask()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SetupCancelled("setup cancelled") from exc


def _require_answer(value: _T | None) -> _T:
    if value is None:
        raise SetupCancelled("setup cancelled")
    return value
