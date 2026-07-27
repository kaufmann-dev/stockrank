from __future__ import annotations

from pathlib import Path

import pytest

from stockrank.config import (
    DEFAULT_SEC_USER_AGENT,
    ConfigError,
    ModelConfig,
    available_names,
    load_app_config,
    load_mode,
    model_config,
    save_model_config,
    set_default_model,
    user_config_path,
    write_toml,
)


def _project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {
                "massive": {},
                "sec": {},
            },
            "data": {"price_history_days": 365, "min_price_bars": 60},
            "defaults": {
                "mode": "best-bet",
                "profile": "medium",
                "seed": 9,
            },
            "tracking": {"benchmark": "spy"},
        },
    )
    write_toml(
        user_config_path(),
        {
            "defaults": {"model": "deepseek"},
            "models": {
                "deepseek": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/",
                    "model": "deepseek-chat",
                    "reasoning_effort": "high",
                    "extra_body": {"thinking": {"type": "enabled"}},
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


def test_loads_named_openai_compatible_profile_and_mode(tmp_path: Path) -> None:
    _project(tmp_path)
    config = load_app_config(tmp_path)
    assert config.defaults.seed == 9
    assert config.tracking.benchmark == "SPY"
    assert config.models["deepseek"].provider == "deepseek"
    assert config.models["deepseek"].base_url == "https://api.deepseek.com"
    assert config.models["deepseek"].reasoning_effort == "high"
    assert config.models["deepseek"].extra_body == {"thinking": {"type": "enabled"}}
    assert config.sec.user_agent == DEFAULT_SEC_USER_AGENT
    assert model_config(tmp_path).model == "deepseek-chat"
    assert load_mode(tmp_path, "best-bet").rank_1_meaning == "best bet"
    assert available_names(tmp_path, "modes") == ["best-bet"]


def test_loads_initialized_config_without_models(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "stockrank.toml",
        {
            "sources": {"massive": {}, "sec": {}},
            "defaults": {"mode": "best-bet", "profile": "medium"},
        },
    )

    config = load_app_config(tmp_path)

    assert config.models == {}
    assert config.defaults.model is None


def test_requires_default_when_models_are_configured(tmp_path: Path) -> None:
    write_toml(
        tmp_path / "stockrank.toml",
        {"sources": {"massive": {}, "sec": {}}, "defaults": {}},
    )
    write_toml(
        user_config_path(),
        {
            "models": {
                "deepseek": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                }
            },
        },
    )

    with pytest.raises(ConfigError, match="global defaults.model is required"):
        load_app_config(tmp_path)


def test_requires_provider_for_every_model_profile(tmp_path: Path) -> None:
    _project(tmp_path)
    raw = {
        "models": {
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            }
        },
    }
    write_toml(user_config_path(), raw)

    with pytest.raises(ConfigError, match="models.deepseek.provider"):
        load_app_config(tmp_path)


def test_rejects_unknown_default_profile_and_mismatched_mode(tmp_path: Path) -> None:
    _project(tmp_path)
    raw = {
        "defaults": {"model": "missing"},
        "models": {
            "deepseek": {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            }
        },
    }
    write_toml(user_config_path(), raw)
    with pytest.raises(ConfigError, match="unknown profile"):
        load_app_config(tmp_path)

    write_toml(
        tmp_path / "modes" / "custom.toml",
        {"name": "other", "rank_1_meaning": "winner", "prompt": "Rank."},
    )
    with pytest.raises(ConfigError, match="declares name"):
        load_mode(tmp_path, "custom")

    write_toml(
        tmp_path / "modes" / "legacy.toml",
        {
            "name": "legacy",
            "rank_1_meaning": "winner",
            "prompt": "Rank.",
            "strategy": "obsolete",
        },
    )
    with pytest.raises(ConfigError, match="unknown fields"):
        load_mode(tmp_path, "legacy")


def test_saves_multiple_profiles_and_changes_default(tmp_path: Path) -> None:
    _project(tmp_path)
    save_model_config(
        ModelConfig(
            name="openrouter",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1/",
            model="provider/model",
        ),
    )
    set_default_model("openrouter")

    config = load_app_config(tmp_path)
    assert sorted(config.models) == ["deepseek", "openrouter"]
    assert config.models["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert config.defaults.model == "openrouter"


def test_rejects_legacy_environment_credential_fields(tmp_path: Path) -> None:
    _project(tmp_path)
    raw = {"sources": {"massive": {"api_key_env": "MASSIVE_KEY"}, "sec": {}}, "defaults": {}}
    write_toml(tmp_path / "stockrank.toml", raw)

    with pytest.raises(ConfigError, match="unknown fields"):
        load_app_config(tmp_path)
