from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .config import DataConfig


class MassiveError(RuntimeError):
    pass


class MassiveClient:
    def __init__(self, api_key: str, base_url: str = "https://api.massive.com", concurrency: int = 8) -> None:
        if not api_key:
            raise MassiveError("MASSIVE_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.concurrency = concurrency

    @classmethod
    def from_env(cls, root: Path) -> "MassiveClient":
        load_dotenv(root / ".env")
        return cls(
            api_key=os.getenv("MASSIVE_API_KEY", ""),
            base_url=os.getenv("MASSIVE_BASE_URL", "https://api.massive.com"),
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        url = f"{self.base_url}{path}"
        last_error: str | None = None
        with httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            for attempt in range(4):
                try:
                    response = client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    time.sleep(0.5 * (2**attempt))
                    continue
                if response.status_code == 403:
                    return {"error": "forbidden for current Massive plan", "status_code": 403, "path": path}
                if response.status_code == 401:
                    raise MassiveError("Massive authentication failed; check MASSIVE_API_KEY")
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    time.sleep(0.5 * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise MassiveError(f"Massive request failed for {path}: HTTP {response.status_code}: {response.text[:200]}")
                return response.json()
        raise MassiveError(f"Massive request failed for {path} after retries: {last_error}")

    def fetch_ticker_bundle(self, ticker: str, data_config: DataConfig) -> dict[str, str]:
        today = datetime.now(UTC).date()
        start = today - timedelta(days=365 * data_config.price_history_years)
        return {
            "details.json": json.dumps(self._get(f"/v3/reference/tickers/{ticker}"), indent=2, sort_keys=True),
            "prices_daily.csv": self._prices_csv(ticker, start, today),
            "financials.json": json.dumps(
                self._get(
                    "/vX/reference/financials",
                    {
                        "ticker": ticker,
                        "timeframe": "quarterly",
                        "limit": data_config.financial_periods,
                        "order": "desc",
                        "sort": "filing_date",
                    },
                ),
                indent=2,
                sort_keys=True,
            ),
            "dividends.json": json.dumps(
                self._get("/v3/reference/dividends", {"ticker": ticker, "limit": 1000, "order": "desc"}),
                indent=2,
                sort_keys=True,
            ),
            "splits.json": json.dumps(
                self._get("/v3/reference/splits", {"ticker": ticker, "limit": 1000, "order": "desc"}),
                indent=2,
                sort_keys=True,
            ),
            "news.json": json.dumps(
                self._get("/v2/reference/news", {"ticker": ticker, "limit": data_config.news_items, "order": "desc"}),
                indent=2,
                sort_keys=True,
            ),
        }

    def fetch_many(self, tickers: list[str], data_config: DataConfig) -> dict[str, dict[str, str]]:
        async def run() -> dict[str, dict[str, str]]:
            semaphore = asyncio.Semaphore(self.concurrency)
            results: dict[str, dict[str, str]] = {}

            async def one(ticker: str) -> None:
                async with semaphore:
                    results[ticker] = await asyncio.to_thread(self.fetch_ticker_bundle, ticker, data_config)

            await asyncio.gather(*(one(ticker) for ticker in tickers))
            return results

        return asyncio.run(run())

    def _prices_csv(self, ticker: str, start: date, end: date) -> str:
        data = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        if "error" in data:
            return json.dumps(data, indent=2, sort_keys=True)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["date", "open", "high", "low", "close", "volume", "vwap", "transactions"])
        writer.writeheader()
        for row in data.get("results", []) or []:
            day = datetime.fromtimestamp(row["t"] / 1000, UTC).date().isoformat()
            writer.writerow(
                {
                    "date": day,
                    "open": row.get("o"),
                    "high": row.get("h"),
                    "low": row.get("l"),
                    "close": row.get("c"),
                    "volume": row.get("v"),
                    "vwap": row.get("vw"),
                    "transactions": row.get("n"),
                }
            )
        return out.getvalue()

    def fetch_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        data = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        if "error" in data:
            return {}
        closes: dict[date, float] = {}
        for row in data.get("results", []) or []:
            closes[datetime.fromtimestamp(row["t"] / 1000, UTC).date()] = float(row["c"])
        return closes
