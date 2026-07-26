from __future__ import annotations

from datetime import date

import httpx
import pytest

from stockrank.massive import (
    MassiveAuthenticationError,
    MassiveClient,
)
from stockrank.sec import SecClient, normalize_cik


def test_massive_generic_pagination_and_structured_entitlement() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/paged" and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA"}],
                    "next_url": "https://api.test/paged?cursor=next",
                },
            )
        if request.url.path == "/paged":
            return httpx.Response(200, json={"results": [{"ticker": "BBB"}]})
        if request.url.path == "/forbidden":
            return httpx.Response(403, json={"error": "not entitled"})
        raise AssertionError(request.url)

    client = MassiveClient(
        "secret",
        "https://api.test",
        transport=httpx.MockTransport(handler),
    )
    result = client.get_paginated("/paged", {"limit": 1})
    assert [row["ticker"] for row in result.records] == ["AAA", "BBB"]
    assert len(result.raw_pages) == 2
    assert result.raw_pages[0]["next_url"].endswith("cursor=next")
    assert "limit" not in requests[1].url.params
    forbidden = client.get_paginated("/forbidden")
    assert forbidden.records == ()
    assert forbidden.coverage_issue is not None
    assert forbidden.coverage_issue.code == "not_entitled"
    assert forbidden.coverage_issue.status_code == 403


def test_massive_401_is_fatal_and_current_endpoint_set_has_no_retired_routes() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/unauthorized":
            return httpx.Response(401, json={"error": "bad key"})
        if request.url.path.startswith("/v3/reference/tickers/"):
            return httpx.Response(
                200,
                json={"results": {"ticker": "AAA", "cik": "1", "active": True}},
            )
        return httpx.Response(200, json={"results": []})

    client = MassiveClient(
        "secret",
        "https://api.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(MassiveAuthenticationError):
        client.get_paginated("/unauthorized")

    client.fetch_ticker_data("AAA", date(2025, 1, 1), date(2026, 1, 1))
    expected = {
        "/v3/reference/tickers/AAA",
        "/v2/aggs/ticker/AAA/range/1/day/2025-01-01/2026-01-01",
        "/v2/reference/news",
        "/stocks/filings/10-K/vX/sections",
        "/stocks/filings/8-K/vX/text",
        "/stocks/filings/8-K/vX/disclosures",
        "/stocks/filings/vX/form-4",
        "/stocks/v1/short-interest",
        "/stocks/v1/dividends",
        "/stocks/v1/splits",
    }
    assert expected <= set(paths)
    assert "/vX/reference/financials" not in paths
    assert "/v3/reference/dividends" not in paths
    assert "/v3/reference/splits" not in paths


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_sec_declares_user_agent_pads_cik_throttles_and_retries() -> None:
    clock = _FakeClock()
    requests: list[httpx.Request] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        requests.append(request)
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"ok": True})

    client = SecClient(
        "stockrank research contact@example.test",
        "https://sec.test",
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
        backoff_seconds=0.25,
    )
    assert client.get_company_facts("320193") == {"ok": True}
    assert client.get_submissions(320193) == {"ok": True}
    assert requests[0].url.path.endswith("/CIK0000320193.json")
    assert requests[-1].url.path == "/submissions/CIK0000320193.json"
    assert all(
        request.headers["user-agent"] == "stockrank research contact@example.test" for request in requests
    )
    assert clock.now >= 0.3
    assert normalize_cik(1) == "0000000001"
    with pytest.raises(ValueError):
        normalize_cik("not-a-cik")


def test_sec_rejects_missing_identity_and_rate_above_policy() -> None:
    with pytest.raises(Exception, match="SEC User-Agent"):
        SecClient("")
    with pytest.raises(ValueError, match=r"\(0, 10\]"):
        SecClient("stockrank contact@example.test", rate_limit_per_second=11)
