from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


class MassiveError(RuntimeError):
    """A fatal Massive API failure."""


class MassiveAuthenticationError(MassiveError):
    """The supplied Massive API key was rejected."""


@dataclass(frozen=True)
class CoverageIssue:
    """A non-fatal source coverage problem, usually a plan entitlement."""

    source: str
    endpoint: str
    status_code: int
    code: str
    message: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "source": self.source,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class MassiveResult:
    """A normalized Massive response for either object or list endpoints."""

    records: tuple[dict[str, Any], ...] = ()
    value: dict[str, Any] | None = None
    coverage_issue: CoverageIssue | None = None
    raw_pages: tuple[dict[str, Any], ...] = ()

    @property
    def available(self) -> bool:
        return self.coverage_issue is None


@dataclass(frozen=True)
class MassiveTickerData:
    ticker: str
    details: dict[str, Any] | None
    daily_bars: tuple[dict[str, Any], ...]
    news: tuple[dict[str, Any], ...]
    ten_k_sections: tuple[dict[str, Any], ...]
    eight_k_text: tuple[dict[str, Any], ...]
    eight_k_disclosures: tuple[dict[str, Any], ...]
    form4: tuple[dict[str, Any], ...]
    short_interest: tuple[dict[str, Any], ...]
    dividends: tuple[dict[str, Any], ...]
    splits: tuple[dict[str, Any], ...]
    coverage_issues: tuple[CoverageIssue, ...]
    raw_responses: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)


class MassiveClient:
    """Small REST client covering only endpoints used by the ranking engine."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise MassiveError("MASSIVE_API_KEY is required")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.transport = transport
        self._sleep = sleep

    @classmethod
    def from_env(cls, root: Path) -> MassiveClient:
        load_dotenv(root / ".env")
        import os

        return cls(
            api_key=os.getenv("MASSIVE_API_KEY", ""),
            base_url=os.getenv("MASSIVE_BASE_URL", "https://api.massive.com"),
        )

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("https://", "http://")):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = f"/{path_or_url}"
        return f"{self.base_url}{path_or_url}"

    def _request_json(
        self,
        path_or_url: str,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, CoverageIssue | None]:
        url = self._url(path_or_url)
        endpoint = httpx.URL(url).path
        last_error = "unknown error"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "stockrank/1",
        }
        with httpx.Client(
            timeout=self.timeout,
            headers=headers,
            transport=self.transport,
            follow_redirects=True,
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    query_params = dict(params) if params is not None else None
                    response = client.get(url, params=query_params)
                except httpx.RequestError as exc:
                    last_error = str(exc)
                    if attempt < self.max_retries:
                        self._sleep(self.backoff_seconds * (2**attempt))
                    continue

                if response.status_code == 401:
                    raise MassiveAuthenticationError("Massive authentication failed; check MASSIVE_API_KEY")
                if response.status_code == 403:
                    return None, CoverageIssue(
                        source="massive",
                        endpoint=endpoint,
                        status_code=403,
                        code="not_entitled",
                        message="endpoint is unavailable on the current Massive plan",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after is not None else None
                        except ValueError:
                            delay = None
                        self._sleep(delay if delay is not None else self.backoff_seconds * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise MassiveError(
                        f"Massive request failed for {endpoint}: "
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MassiveError(f"Massive returned invalid JSON for {endpoint}") from exc
                if not isinstance(payload, dict):
                    raise MassiveError(f"Massive returned a non-object response for {endpoint}")
                return payload, None
        raise MassiveError(
            f"Massive request failed for {endpoint} after {self.max_retries + 1} attempts: {last_error}"
        )

    def get_object(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> MassiveResult:
        payload, issue = self._request_json(path, params)
        if issue is not None:
            return MassiveResult(coverage_issue=issue)
        assert payload is not None
        value = payload.get("results")
        if value is None:
            return MassiveResult(value={}, raw_pages=(payload,))
        if not isinstance(value, dict):
            raise MassiveError(f"Massive returned non-object results for {path}")
        return MassiveResult(value=dict(value), raw_pages=(payload,))

    def get_paginated(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_records: int | None = None,
        max_pages: int = 1_000,
    ) -> MassiveResult:
        """Fetch every `next_url` page, preserving a partial page on entitlement loss."""

        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be positive")
        records: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        next_target: str | None = path
        next_params: Mapping[str, Any] | None = params
        seen: set[str] = set()
        pages = 0
        while next_target is not None:
            absolute = self._url(next_target)
            if absolute in seen:
                raise MassiveError(f"Massive pagination loop detected for {path}")
            if pages >= max_pages:
                raise MassiveError(f"Massive pagination exceeded {max_pages} pages for {path}")
            seen.add(absolute)
            pages += 1
            payload, issue = self._request_json(next_target, next_params)
            if issue is not None:
                return MassiveResult(
                    records=tuple(records),
                    coverage_issue=issue,
                    raw_pages=tuple(raw_pages),
                )
            assert payload is not None
            raw_pages.append(payload)
            raw_records = payload.get("results", [])
            if raw_records is None:
                raw_records = []
            if not isinstance(raw_records, list) or not all(
                isinstance(record, dict) for record in raw_records
            ):
                raise MassiveError(f"Massive returned non-list results for {path}")
            records.extend(dict(record) for record in raw_records)
            if max_records is not None and len(records) >= max_records:
                records = records[:max_records]
                break
            raw_next = payload.get("next_url")
            if raw_next is not None and not isinstance(raw_next, str):
                raise MassiveError(f"Massive returned an invalid next_url for {path}")
            next_target = raw_next
            next_params = None
        return MassiveResult(records=tuple(records), raw_pages=tuple(raw_pages))

    def get_ticker_details(self, ticker: str, as_of: date | None = None) -> MassiveResult:
        params = {"date": as_of.isoformat()} if as_of is not None else None
        symbol = quote(ticker.upper(), safe=".-")
        return self.get_object(f"/v3/reference/tickers/{symbol}", params)

    def list_tickers(self, as_of: date) -> MassiveResult:
        return self.get_paginated(
            "/v3/reference/tickers",
            {
                "active": "true",
                "date": as_of.isoformat(),
                "locale": "us",
                "market": "stocks",
                "type": "CS",
                "limit": 1000,
                "sort": "ticker",
                "order": "asc",
            },
        )

    def get_daily_bars(self, ticker: str, start: date, end: date) -> MassiveResult:
        symbol = quote(ticker.upper(), safe=".-")
        return self.get_paginated(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50_000},
        )

    def get_grouped_daily(self, day: date) -> MassiveResult:
        return self.get_paginated(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": "true", "include_otc": "false"},
        )

    def get_news(self, ticker: str, as_of: date, *, limit: int = 10) -> MassiveResult:
        return self.get_paginated(
            "/v2/reference/news",
            {
                "ticker": ticker.upper(),
                "published_utc.lte": f"{as_of.isoformat()}T23:59:59Z",
                "sort": "published_utc",
                "order": "desc",
                "limit": min(limit, 1000),
            },
            max_records=limit,
        )

    def get_ten_k_sections(self, ticker: str, as_of: date, *, limit: int = 10) -> MassiveResult:
        return self.get_paginated(
            "/stocks/filings/10-K/vX/sections",
            {
                "ticker": ticker.upper(),
                "filing_date.lte": as_of.isoformat(),
                "sort": "filing_date.desc",
                "limit": min(limit, 100),
            },
            max_records=limit,
        )

    def get_eight_k_text(self, ticker: str, as_of: date, *, limit: int = 10) -> MassiveResult:
        return self.get_paginated(
            "/stocks/filings/8-K/vX/text",
            {
                "ticker": ticker.upper(),
                "filing_date.lte": as_of.isoformat(),
                "sort": "filing_date.desc",
                "limit": min(limit, 100),
            },
            max_records=limit,
        )

    def get_eight_k_disclosures(self, ticker: str, as_of: date, *, limit: int = 10) -> MassiveResult:
        return self.get_paginated(
            "/stocks/filings/8-K/vX/disclosures",
            {
                "ticker": ticker.upper(),
                "filing_date.lte": as_of.isoformat(),
                "sort": "filing_date.desc",
                "limit": min(limit, 100),
            },
            max_records=limit,
        )

    def get_form4(self, ticker: str, as_of: date, *, limit: int = 25) -> MassiveResult:
        return self.get_paginated(
            "/stocks/filings/vX/form-4",
            {
                "tickers": ticker.upper(),
                "filing_date.lte": as_of.isoformat(),
                "sort": "filing_date.desc",
                "limit": min(limit, 10_000),
            },
            max_records=limit,
        )

    def get_short_interest(self, ticker: str, as_of: date, *, limit: int = 10) -> MassiveResult:
        return self.get_paginated(
            "/stocks/v1/short-interest",
            {
                "ticker": ticker.upper(),
                "settlement_date.lte": as_of.isoformat(),
                "sort": "settlement_date.desc",
                "limit": min(limit, 50_000),
            },
            max_records=limit,
        )

    def get_dividends(self, ticker: str, as_of: date, *, limit: int = 25) -> MassiveResult:
        return self.get_paginated(
            "/stocks/v1/dividends",
            {
                "ticker": ticker.upper(),
                "ex_dividend_date.lte": as_of.isoformat(),
                "sort": "ex_dividend_date.desc",
                "limit": min(limit, 5_000),
            },
            max_records=limit,
        )

    def get_splits(self, ticker: str, as_of: date, *, limit: int = 25) -> MassiveResult:
        return self.get_paginated(
            "/stocks/v1/splits",
            {
                "ticker": ticker.upper(),
                "execution_date.lte": as_of.isoformat(),
                "sort": "execution_date.desc",
                "limit": min(limit, 5_000),
            },
            max_records=limit,
        )

    def fetch_ticker_data(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        news_limit: int = 10,
        filing_limit: int = 10,
        insider_limit: int = 25,
    ) -> MassiveTickerData:
        ticker = ticker.upper()
        endpoints = {
            "details": self.get_ticker_details(ticker, end),
            "daily_bars": self.get_daily_bars(ticker, start, end),
            "news": self.get_news(ticker, end, limit=news_limit),
            "ten_k_sections": self.get_ten_k_sections(ticker, end, limit=filing_limit),
            "eight_k_text": self.get_eight_k_text(ticker, end, limit=filing_limit),
            "eight_k_disclosures": self.get_eight_k_disclosures(ticker, end, limit=filing_limit),
            "form4": self.get_form4(ticker, end, limit=insider_limit),
            "short_interest": self.get_short_interest(ticker, end),
            "dividends": self.get_dividends(ticker, end),
            "splits": self.get_splits(ticker, end),
        }
        issues = tuple(
            result.coverage_issue for result in endpoints.values() if result.coverage_issue is not None
        )
        details = endpoints["details"].value
        return MassiveTickerData(
            ticker=ticker,
            details=details,
            daily_bars=endpoints["daily_bars"].records,
            news=endpoints["news"].records,
            ten_k_sections=endpoints["ten_k_sections"].records,
            eight_k_text=endpoints["eight_k_text"].records,
            eight_k_disclosures=endpoints["eight_k_disclosures"].records,
            form4=endpoints["form4"].records,
            short_interest=endpoints["short_interest"].records,
            dividends=endpoints["dividends"].records,
            splits=endpoints["splits"].records,
            coverage_issues=issues,
            raw_responses={name: result.raw_pages for name, result in endpoints.items()},
        )

    def fetch_adjusted_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        """Return adjusted closes for forward-performance tracking."""

        result = self.get_daily_bars(ticker, start, end)
        if result.coverage_issue is not None:
            return {}
        closes: dict[date, float] = {}
        for row in result.records:
            timestamp = row.get("t")
            close = row.get("c")
            if not isinstance(timestamp, int | float) or not isinstance(close, int | float):
                continue
            day = datetime.fromtimestamp(float(timestamp) / 1000, UTC).date()
            closes[day] = float(close)
        return closes
