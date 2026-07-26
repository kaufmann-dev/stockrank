from __future__ import annotations

import httpx
import pytest

from stockrank.model_catalog import CatalogError, ModelsDevClient, parse_models_dev_catalog


def _text_model(name: str, *, status: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "modalities": {"input": ["text"], "output": ["text"]},
    }
    if status is not None:
        value["status"] = status
    return value


def test_parses_only_openai_compatible_text_catalog_entries() -> None:
    providers = parse_models_dev_catalog(
        {
            "deepseek": {
                "name": "DeepSeek",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://api.deepseek.com/",
                "models": {
                    "deepseek-v4-pro": _text_model("DeepSeek V4 Pro"),
                    "legacy": _text_model("Legacy", status="deprecated"),
                    "image": {
                        "name": "Image",
                        "modalities": {"input": ["text"], "output": ["image"]},
                    },
                },
            },
            "openai": {
                "name": "OpenAI",
                "npm": "@ai-sdk/openai",
                "models": {"gpt-5": _text_model("GPT-5")},
            },
            "openrouter": {
                "name": "OpenRouter",
                "npm": "@openrouter/ai-sdk-provider",
                "api": "https://openrouter.ai/api/v1",
                "models": {
                    "anthropic/claude": _text_model("Claude", status="beta"),
                },
            },
            "anthropic": {
                "name": "Anthropic",
                "npm": "@ai-sdk/anthropic",
                "api": "https://api.anthropic.com",
                "models": {"claude": _text_model("Claude")},
            },
            "templated": {
                "name": "Templated",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://${ACCOUNT}.example.com/v1",
                "models": {"model": _text_model("Model")},
            },
        }
    )

    assert [provider.id for provider in providers] == ["deepseek", "openai", "openrouter"]
    assert providers[0].base_url == "https://api.deepseek.com"
    assert [model.id for model in providers[0].models] == ["deepseek-v4-pro"]
    assert providers[1].base_url == "https://api.openai.com/v1"
    assert providers[2].models[0].status == "beta"


def test_rejects_catalog_without_compatible_providers() -> None:
    with pytest.raises(CatalogError, match="no compatible"):
        parse_models_dev_catalog({"anthropic": {"name": "Anthropic", "models": {}}})


def test_client_normalizes_http_and_json_failures() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with (
        httpx.Client(transport=httpx.MockTransport(unavailable)) as client,
        pytest.raises(CatalogError, match="catalog is unavailable"),
    ):
        ModelsDevClient(client).providers()

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with (
        httpx.Client(transport=httpx.MockTransport(malformed)) as client,
        pytest.raises(CatalogError, match="catalog is unavailable"),
    ):
        ModelsDevClient(client).providers()
