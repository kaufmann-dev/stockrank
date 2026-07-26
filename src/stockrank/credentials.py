from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "stockrank"
MASSIVE_ACCOUNT = "massive.com:api-key"
LLM_ACCOUNT_PREFIX = "llm:"


class CredentialError(RuntimeError):
    """Raised when a credential is missing or the system keyring is unavailable."""


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class CredentialStore:
    """Store API credentials in the operating system keyring."""

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = backend or keyring.get_keyring()

    def set_massive_key(self, api_key: str) -> None:
        self._set(MASSIVE_ACCOUNT, api_key, "Massive API key")

    def get_massive_key(self) -> str | None:
        return self._get(MASSIVE_ACCOUNT)

    def require_massive_key(self) -> str:
        return self._require(MASSIVE_ACCOUNT, "Massive API key")

    def delete_massive_key(self) -> bool:
        return self._delete(MASSIVE_ACCOUNT)

    def set_llm_key(self, profile_name: str, api_key: str) -> None:
        self._set(_llm_account(profile_name), api_key, f"LLM profile {profile_name!r} API key")

    def get_llm_key(self, profile_name: str) -> str | None:
        return self._get(_llm_account(profile_name))

    def require_llm_key(self, profile_name: str) -> str:
        return self._require(
            _llm_account(profile_name),
            f"API key for LLM profile {profile_name!r}",
        )

    def delete_llm_key(self, profile_name: str) -> bool:
        return self._delete(_llm_account(profile_name))

    def _set(self, account: str, secret: str, label: str) -> None:
        value = secret.strip()
        if not value:
            raise CredentialError(f"{label} must not be empty")
        try:
            self._backend.set_password(SERVICE_NAME, account, value)
        except KeyringError as exc:
            raise _backend_error(exc) from exc

    def _get(self, account: str) -> str | None:
        try:
            value = self._backend.get_password(SERVICE_NAME, account)
        except KeyringError as exc:
            raise _backend_error(exc) from exc
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def _require(self, account: str, label: str) -> str:
        value = self._get(account)
        if value is None:
            raise CredentialError(f"{label} is not stored; save it with the stockrank CLI first")
        return value

    def _delete(self, account: str) -> bool:
        if self._get(account) is None:
            return False
        try:
            self._backend.delete_password(SERVICE_NAME, account)
        except KeyringError as exc:
            raise _backend_error(exc) from exc
        return True


def _llm_account(profile_name: str) -> str:
    value = profile_name.strip()
    if not value:
        raise CredentialError("LLM profile name must not be empty")
    return f"{LLM_ACCOUNT_PREFIX}{value}"


def _backend_error(exc: KeyringError) -> CredentialError:
    return CredentialError(
        "the operating system keyring is unavailable or locked; "
        f"configure a supported keyring backend ({type(exc).__name__}: {exc})"
    )
