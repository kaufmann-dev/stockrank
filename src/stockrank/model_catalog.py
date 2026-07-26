from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

MODELS_DEV_URL = "https://models.dev/api.json"
_OPENAI_COMPATIBLE_NPM = "@ai-sdk/openai-compatible"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PROVIDER_BASE_URL_OVERRIDES = {
    "openai": "https://api.openai.com/v1",
}
_COMPATIBLE_PROVIDER_PACKAGES = {
    _OPENAI_COMPATIBLE_NPM,
    "@openrouter/ai-sdk-provider",
}


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str
    status: str | None = None


@dataclass(frozen=True)
class CatalogProvider:
    id: str
    name: str
    base_url: str
    models: tuple[CatalogModel, ...]


class ModelsDevClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        url: str = MODELS_DEV_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._url = url
        self._timeout_seconds = timeout_seconds

    def providers(self) -> tuple[CatalogProvider, ...]:
        try:
            if self._client is not None:
                return self._request(self._client)
            with httpx.Client(timeout=self._timeout_seconds, follow_redirects=True) as client:
                return self._request(client)
        except CatalogError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogError(f"models.dev catalog is unavailable: {exc}") from exc

    def _request(self, client: httpx.Client) -> tuple[CatalogProvider, ...]:
        response = client.get(self._url)
        response.raise_for_status()
        return parse_models_dev_catalog(response.json())


def parse_models_dev_catalog(raw: object) -> tuple[CatalogProvider, ...]:
    if not isinstance(raw, Mapping):
        raise CatalogError("models.dev catalog must be a JSON object")

    providers: list[CatalogProvider] = []
    for provider_id, provider_raw in raw.items():
        if not isinstance(provider_id, str) or not _IDENTIFIER_RE.fullmatch(provider_id):
            continue
        if not isinstance(provider_raw, Mapping):
            continue
        base_url = _provider_base_url(provider_id, provider_raw)
        if base_url is None:
            continue
        name = provider_raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        models_raw = provider_raw.get("models")
        if not isinstance(models_raw, Mapping):
            continue
        models = _catalog_models(models_raw)
        if not models:
            continue
        providers.append(
            CatalogProvider(
                id=provider_id,
                name=name.strip(),
                base_url=base_url,
                models=models,
            )
        )

    if not providers:
        raise CatalogError("models.dev catalog contains no compatible text-model providers")
    return tuple(sorted(providers, key=lambda provider: (provider.name.casefold(), provider.id)))


def _provider_base_url(provider_id: str, raw: Mapping[object, object]) -> str | None:
    npm = raw.get("npm")
    if provider_id not in _PROVIDER_BASE_URL_OVERRIDES and npm not in _COMPATIBLE_PROVIDER_PACKAGES:
        return None
    value = _PROVIDER_BASE_URL_OVERRIDES.get(provider_id, raw.get("api"))
    if not isinstance(value, str) or "${" in value:
        return None
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return normalized


def _catalog_models(raw: Mapping[object, object]) -> tuple[CatalogModel, ...]:
    models: list[CatalogModel] = []
    for model_id, model_raw in raw.items():
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        if not isinstance(model_raw, Mapping) or not _supports_text(model_raw):
            continue
        status = model_raw.get("status")
        if status == "deprecated":
            continue
        if status is not None and not isinstance(status, str):
            continue
        name = model_raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        models.append(
            CatalogModel(
                id=model_id.strip(),
                name=name.strip(),
                status=status,
            )
        )
    return tuple(sorted(models, key=lambda model: (model.name.casefold(), model.id)))


def _supports_text(raw: Mapping[object, object]) -> bool:
    modalities = raw.get("modalities")
    if not isinstance(modalities, Mapping):
        return False
    inputs = modalities.get("input")
    outputs = modalities.get("output")
    return _string_sequence_contains(inputs, "text") and _string_sequence_contains(outputs, "text")


def _string_sequence_contains(raw: Any, expected: str) -> bool:
    return isinstance(raw, list) and all(isinstance(value, str) for value in raw) and expected in raw
