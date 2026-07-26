from __future__ import annotations

import pytest
from keyring.errors import NoKeyringError

from stockrank.credentials import SERVICE_NAME, CredentialError, CredentialStore


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        del service, username
        raise NoKeyringError("no backend")


def test_stores_multiple_llm_keys_and_massive_separately() -> None:
    backend = MemoryKeyring()
    store = CredentialStore(backend)

    store.set_llm_key("deepseek", "deepseek-secret")
    store.set_llm_key("openrouter", "openrouter-secret")
    store.set_massive_key("massive-secret")

    assert store.require_llm_key("deepseek") == "deepseek-secret"
    assert store.require_llm_key("openrouter") == "openrouter-secret"
    assert store.require_massive_key() == "massive-secret"
    assert all(service == SERVICE_NAME for service, _account in backend.values)


def test_missing_and_deleted_credentials_are_explicit() -> None:
    store = CredentialStore(MemoryKeyring())

    with pytest.raises(CredentialError, match="not stored"):
        store.require_llm_key("missing")
    assert not store.delete_massive_key()

    store.set_massive_key("secret")
    assert store.delete_massive_key()
    assert store.get_massive_key() is None


def test_backend_errors_do_not_fall_back_to_plaintext() -> None:
    store = CredentialStore(BrokenKeyring())

    with pytest.raises(CredentialError, match="keyring is unavailable"):
        store.get_massive_key()
