from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx


class SecError(RuntimeError):
    """A fatal SEC data API failure."""


def normalize_cik(cik: str | int) -> str:
    text = str(cik).strip()
    if not text.isdigit() or len(text) > 10:
        raise ValueError("CIK must contain at most 10 decimal digits")
    return text.zfill(10)


class SecClient:
    """Rate-limited client for the SEC companyfacts and submissions APIs."""

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://data.sec.gov",
        *,
        rate_limit_per_second: float = 10.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not user_agent.strip():
            raise SecError("SEC User-Agent is required and must identify the application")
        if not 0 < rate_limit_per_second <= 10:
            raise ValueError("SEC rate_limit_per_second must be in (0, 10]")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.user_agent = user_agent.strip()
        self.base_url = base_url.rstrip("/")
        self.rate_limit_per_second = rate_limit_per_second
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.transport = transport
        self._sleep = sleep
        self._clock = clock
        self._interval = 1.0 / rate_limit_per_second
        self._next_request_at = 0.0
        self._throttle_lock = threading.Lock()

    def _throttle(self) -> None:
        with self._throttle_lock:
            now = self._clock()
            wait = max(0.0, self._next_request_at - now)
            if wait:
                self._sleep(wait)
                now = self._clock()
            self._next_request_at = max(now, self._next_request_at) + self._interval

    def _get(self, path: str) -> dict[str, Any]:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": self.user_agent,
        }
        last_error = "unknown error"
        with httpx.Client(
            timeout=self.timeout,
            headers=headers,
            transport=self.transport,
            follow_redirects=True,
        ) as client:
            for attempt in range(self.max_retries + 1):
                self._throttle()
                try:
                    response = client.get(url)
                except httpx.RequestError as exc:
                    last_error = str(exc)
                    if attempt < self.max_retries:
                        self._sleep(self.backoff_seconds * (2**attempt))
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after else None
                        except ValueError:
                            delay = None
                        self._sleep(delay if delay is not None else self.backoff_seconds * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise SecError(
                        f"SEC request failed for {path}: HTTP {response.status_code}: {response.text[:200]}"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SecError(f"SEC returned invalid JSON for {path}") from exc
                if not isinstance(payload, dict):
                    raise SecError(f"SEC returned a non-object response for {path}")
                return payload
        raise SecError(f"SEC request failed for {path} after {self.max_retries + 1} attempts: {last_error}")

    def get_company_facts(self, cik: str | int) -> dict[str, Any]:
        return self._get(f"/api/xbrl/companyfacts/CIK{normalize_cik(cik)}.json")

    def get_submissions(self, cik: str | int) -> dict[str, Any]:
        return self._get(f"/submissions/CIK{normalize_cik(cik)}.json")
