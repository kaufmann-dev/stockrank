from __future__ import annotations

from pathlib import Path

import pytest

from stockrank.config import (
    ConfigError,
    available_names,
    load_app_config,
    load_mode,
    model_config,
    require_environment,
    write_toml,
)


def _project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {
                "massive": {"api_key_env": "MASSIVE_KEY"},
                "sec": {"user_agent_env": "SEC_AGENT"},
            },
            "data": {"price_history_days": 365, "min_price_bars": 60},
            "defaults": {
                "model": "deepseek",
                "mode": "best-bet",
                "profile": "medium",
                "seed": 9,
            },
            "tracking": {"benchmark": "spy"},
            "models": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/",
                    "model": "deepseek-chat",
                    "api_key_env": "DEEPSEEK_KEY",
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
    assert config.models["deepseek"].base_url == "https://api.deepseek.com"
    assert config.models["deepseek"].reasoning_effort == "high"
    assert config.models["deepseek"].extra_body == {"thinking": {"type": "enabled"}}
    assert model_config(tmp_path).model == "deepseek-chat"
    assert load_mode(tmp_path, "best-bet").rank_1_meaning == "best bet"
    assert available_names(tmp_path, "modes") == ["best-bet"]


def test_rejects_unknown_default_profile_and_mismatched_mode(tmp_path: Path) -> None:
    _project(tmp_path)
    raw = {
        "sources": {
            "massive": {"api_key_env": "MASSIVE_KEY"},
            "sec": {"user_agent_env": "SEC_AGENT"},
        },
        "defaults": {"model": "missing"},
        "models": {
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "api_key_env": "DEEPSEEK_KEY",
            }
        },
    }
    write_toml(tmp_path / "stockrank.toml", raw)
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


def test_requires_environment_without_exposing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_STOCKRANK_SECRET", raising=False)
    with pytest.raises(ConfigError, match="MISSING_STOCKRANK_SECRET"):
        require_environment("MISSING_STOCKRANK_SECRET")
    monkeypatch.setenv("MISSING_STOCKRANK_SECRET", "private")
    assert require_environment("MISSING_STOCKRANK_SECRET") == "private"
