from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any, Protocol

from .massive import CoverageIssue, MassiveTickerData
from .sec import SecError


@dataclass(frozen=True)
class EvidenceSettings:
    price_history_days: int = 730
    min_price_bars: int = 60
    news_items: int = 20
    filing_items: int = 10
    insider_items: int = 20
    evidence_char_budget: int = 12_000


DEFAULT_EVIDENCE_SETTINGS = EvidenceSettings()


@dataclass(frozen=True)
class EvidenceCoverageIssue:
    source: str
    endpoint: str
    code: str
    message: str
    status_code: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "source": self.source,
            "endpoint": self.endpoint,
            "code": self.code,
            "message": _sanitize_text(self.message, limit=240),
            "status_code": self.status_code,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceCoverageIssue:
        status = raw.get("status_code")
        return cls(
            source=str(raw.get("source", "")),
            endpoint=str(raw.get("endpoint", "")),
            code=str(raw.get("code", "")),
            message=str(raw.get("message", "")),
            status_code=int(status) if isinstance(status, int | float) else None,
        )


_MASSIVE_OBJECT_RESPONSES = ("details",)
_MASSIVE_LIST_RESPONSES = (
    "daily_bars",
    "news",
    "ten_k_sections",
    "eight_k_text",
    "eight_k_disclosures",
    "form4",
    "short_interest",
    "dividends",
    "splits",
)
_MASSIVE_RESPONSES = (
    *_MASSIVE_OBJECT_RESPONSES,
    *_MASSIVE_LIST_RESPONSES,
)


@dataclass(frozen=True)
class TickerSourceBundle:
    """Raw, JSON-serializable inputs checkpointed before evidence derivation."""

    ticker: str
    as_of: date
    price_start: date
    massive: MassiveTickerData
    sec_companyfacts: dict[str, Any] | None
    sec_submissions: dict[str, Any] | None
    coverage_issues: tuple[EvidenceCoverageIssue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        responses: dict[str, list[dict[str, Any]]] = {}
        for name in _MASSIVE_RESPONSES:
            if name in self.massive.raw_responses:
                responses[name] = [dict(page) for page in self.massive.raw_responses[name]]
                continue
            value: object
            if name == "details":
                value = self.massive.details or {}
            else:
                value = list(getattr(self.massive, name))
            responses[name] = [{"results": value}]
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "price_start": self.price_start.isoformat(),
            "massive": {
                "ticker": self.massive.ticker,
                "responses": responses,
                "coverage_issues": [issue.as_dict() for issue in self.massive.coverage_issues],
            },
            "sec_companyfacts": self.sec_companyfacts,
            "sec_submissions": self.sec_submissions,
            "coverage_issues": [issue.as_dict() for issue in self.coverage_issues],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TickerSourceBundle:
        massive_raw = raw.get("massive")
        if not isinstance(massive_raw, dict):
            raise TypeError("source bundle massive field must be an object")
        massive_issues_raw = massive_raw.get("coverage_issues", [])
        if not isinstance(massive_issues_raw, list):
            raise TypeError("source bundle Massive coverage_issues must be an array")
        massive_issues = tuple(
            CoverageIssue(
                source=str(item.get("source", "massive")),
                endpoint=str(item.get("endpoint", "")),
                status_code=int(item.get("status_code", 0)),
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
            )
            for item in massive_issues_raw
            if isinstance(item, dict)
        )

        responses_raw = massive_raw.get("responses")
        if not isinstance(responses_raw, dict):
            raise TypeError("source bundle Massive responses must be an object")
        unexpected = sorted(set(responses_raw) - set(_MASSIVE_RESPONSES))
        if unexpected:
            raise ValueError(
                "source bundle Massive responses contain unknown endpoints: " + ", ".join(unexpected)
            )

        def pages(name: str) -> tuple[dict[str, Any], ...]:
            value = responses_raw.get(name)
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise TypeError(f"source bundle Massive response {name} must be an array")
            return tuple(dict(item) for item in value)

        def records(name: str) -> tuple[dict[str, Any], ...]:
            flattened: list[dict[str, Any]] = []
            for page in pages(name):
                value = page.get("results", [])
                if value is None:
                    continue
                if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                    raise TypeError(f"source bundle Massive {name} results must be an array")
                flattened.extend(dict(item) for item in value)
            return tuple(flattened)

        details_pages = pages("details")
        details: dict[str, Any] | None = None
        if details_pages:
            details_value = details_pages[0].get("results")
            if details_value is not None and not isinstance(details_value, dict):
                raise TypeError("source bundle Massive details results must be an object")
            if isinstance(details_value, dict):
                details = dict(details_value)
        coverage_raw = raw.get("coverage_issues", [])
        if not isinstance(coverage_raw, list):
            raise TypeError("source bundle coverage_issues must be an array")
        companyfacts = raw.get("sec_companyfacts")
        submissions = raw.get("sec_submissions")
        if companyfacts is not None and not isinstance(companyfacts, dict):
            raise ValueError("source bundle sec_companyfacts must be an object")
        if submissions is not None and not isinstance(submissions, dict):
            raise ValueError("source bundle sec_submissions must be an object")
        return cls(
            ticker=str(raw["ticker"]).upper(),
            as_of=date.fromisoformat(str(raw["as_of"])),
            price_start=date.fromisoformat(str(raw["price_start"])),
            massive=MassiveTickerData(
                ticker=str(massive_raw.get("ticker", raw["ticker"])).upper(),
                details=details,
                daily_bars=records("daily_bars"),
                news=records("news"),
                ten_k_sections=records("ten_k_sections"),
                eight_k_text=records("eight_k_text"),
                eight_k_disclosures=records("eight_k_disclosures"),
                form4=records("form4"),
                short_interest=records("short_interest"),
                dividends=records("dividends"),
                splits=records("splits"),
                coverage_issues=massive_issues,
                raw_responses={name: pages(name) for name in _MASSIVE_RESPONSES},
            ),
            sec_companyfacts=dict(companyfacts) if isinstance(companyfacts, dict) else None,
            sec_submissions=dict(submissions) if isinstance(submissions, dict) else None,
            coverage_issues=tuple(
                EvidenceCoverageIssue.from_dict(item) for item in coverage_raw if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    category: str
    knowledge_date: date
    summary: str
    metrics: dict[str, float] = field(default_factory=dict)
    percentiles: dict[str, float] = field(default_factory=dict)
    source: str = ""
    source_url: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.evidence_id,
            "category": self.category,
            "knowledge_date": self.knowledge_date.isoformat(),
            "summary": self.summary,
        }
        if self.metrics:
            result["metrics"] = {key: round(value, 8) for key, value in sorted(self.metrics.items())}
        if self.percentiles:
            result["universe_percentiles"] = {
                key: round(value, 6) for key, value in sorted(self.percentiles.items())
            }
        if self.source:
            result["source"] = self.source
        if self.source_url:
            result["source_url"] = self.source_url
        return result

    def as_dict(self) -> dict[str, Any]:
        result = self.as_prompt_dict()
        if self.metrics:
            result["metrics"] = dict(sorted(self.metrics.items()))
        if self.percentiles:
            result["universe_percentiles"] = dict(sorted(self.percentiles.items()))
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceItem:
        metrics = raw.get("metrics", {})
        percentiles = raw.get("universe_percentiles", raw.get("percentiles", {}))
        if not isinstance(metrics, dict) or not isinstance(percentiles, dict):
            raise TypeError("evidence metrics and percentiles must be objects")

        def numeric_map(values: dict[str, Any], label: str) -> dict[str, float]:
            parsed: dict[str, float] = {}
            for key, value in values.items():
                number = _number(value)
                if not isinstance(key, str) or number is None:
                    raise ValueError(f"evidence {label} must contain finite numbers")
                parsed[key] = number
            return parsed

        evidence_id = raw.get("id", raw.get("evidence_id"))
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("evidence item id must be a non-empty string")
        category = raw.get("category")
        summary = raw.get("summary")
        knowledge_date = _parse_date(raw.get("knowledge_date"))
        if (
            not isinstance(category, str)
            or not category
            or not isinstance(summary, str)
            or not summary
            or knowledge_date is None
        ):
            raise ValueError("evidence item category, knowledge_date, and summary are required")
        source = raw.get("source", "")
        source_url = raw.get("source_url")
        if not isinstance(source, str) or (source_url is not None and not isinstance(source_url, str)):
            raise ValueError("evidence source fields must be strings")
        return cls(
            evidence_id=evidence_id,
            category=category,
            knowledge_date=knowledge_date,
            summary=summary,
            metrics=numeric_map(metrics, "metrics"),
            percentiles=numeric_map(percentiles, "percentiles"),
            source=source,
            source_url=source_url,
        )


@dataclass(frozen=True)
class EvidencePack:
    ticker: str
    as_of: date
    eligible: bool
    exclusion_reason: str | None
    bar_count: int
    substantive_categories: tuple[str, ...]
    items: tuple[EvidenceItem, ...]
    coverage_issues: tuple[EvidenceCoverageIssue, ...] = ()

    @property
    def allowed_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "eligible": self.eligible,
            "exclusion_reason": self.exclusion_reason,
            "bar_count": self.bar_count,
            "substantive_categories": list(self.substantive_categories),
            "coverage_warnings": [issue.as_dict() for issue in self.coverage_issues],
            "evidence": [item.as_prompt_dict() for item in self.items],
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.as_prompt_dict()
        result["evidence"] = [item.as_dict() for item in self.items]
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidencePack:
        ticker = raw.get("ticker")
        as_of = _parse_date(raw.get("as_of"))
        eligible = raw.get("eligible")
        exclusion_reason = raw.get("exclusion_reason")
        bar_count = raw.get("bar_count")
        categories = raw.get("substantive_categories")
        items = raw.get("evidence", raw.get("items"))
        coverage = raw.get("coverage_warnings", raw.get("coverage_issues", []))
        if not isinstance(ticker, str) or not ticker or as_of is None:
            raise ValueError("evidence pack ticker and as_of are required")
        if not isinstance(eligible, bool):
            raise TypeError("evidence pack eligible must be boolean")
        if exclusion_reason is not None and not isinstance(exclusion_reason, str):
            raise ValueError("evidence pack exclusion_reason must be a string")
        if isinstance(bar_count, bool) or not isinstance(bar_count, int) or bar_count < 0:
            raise ValueError("evidence pack bar_count must be non-negative")
        if not isinstance(categories, list) or not all(isinstance(item, str) for item in categories):
            raise ValueError("evidence pack substantive_categories must be an array")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("evidence pack evidence must be an array")
        if not isinstance(coverage, list) or not all(isinstance(item, dict) for item in coverage):
            raise ValueError("evidence pack coverage_warnings must be an array")
        parsed_items = tuple(EvidenceItem.from_dict(item) for item in items)
        ids = [item.evidence_id for item in parsed_items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence pack contains duplicate evidence IDs")
        if any(item.knowledge_date > as_of for item in parsed_items):
            raise ValueError("evidence pack contains future-dated evidence")
        return cls(
            ticker=ticker.upper(),
            as_of=as_of,
            eligible=eligible,
            exclusion_reason=exclusion_reason,
            bar_count=bar_count,
            substantive_categories=tuple(categories),
            items=parsed_items,
            coverage_issues=tuple(EvidenceCoverageIssue.from_dict(item) for item in coverage),
        )


@dataclass(frozen=True)
class FactPoint:
    tag: str
    value: float
    start: date | None
    end: date
    filed: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession_number: str | None


class MassiveEvidenceSource(Protocol):
    def fetch_ticker_data(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        news_limit: int = 10,
        filing_limit: int = 10,
        insider_limit: int = 25,
    ) -> MassiveTickerData: ...


class SecEvidenceSource(Protocol):
    def get_company_facts(self, cik: str | int) -> dict[str, Any]: ...

    def get_submissions(self, cik: str | int) -> dict[str, Any]: ...


_CURRENCY_AMOUNT_RE = re.compile(
    r"(?i)(?:US\$|\$|USD\s*)\s*\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:thousand|million|billion|trillion|[KMBT]))?"
)
_DOLLARS_RE = re.compile(
    r"(?i)\b\d[\d,]*(?:\.\d+)?\s*"
    r"(?:thousand|million|billion|trillion)?\s+(?:U\.S\.\s+)?dollars?\b"
)
_SPACE_RE = re.compile(r"\s+")


def _sanitize_text(value: object, *, limit: int = 640) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    text = _CURRENCY_AMOUNT_RE.sub("[monetary amount omitted]", text)
    text = _DOLLARS_RE.sub("[monetary amount omitted]", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _stable_id(ticker: str, category: str, knowledge_date: date, source_key: str) -> str:
    material = json.dumps(
        [ticker.upper(), category, knowledge_date.isoformat(), source_key],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{ticker.upper()}:{category}:{digest}"


def _item(
    ticker: str,
    category: str,
    knowledge_date: date,
    source_key: str,
    summary: str,
    *,
    metrics: dict[str, float] | None = None,
    source: str,
    source_url: str | None = None,
) -> EvidenceItem:
    clean_metrics = {
        key: float(value) for key, value in sorted((metrics or {}).items()) if math.isfinite(float(value))
    }
    return EvidenceItem(
        evidence_id=_stable_id(ticker, category, knowledge_date, source_key),
        category=category,
        knowledge_date=knowledge_date,
        summary=_sanitize_text(summary),
        metrics=clean_metrics,
        source=source,
        source_url=source_url,
    )


def _coverage_from_massive(issue: CoverageIssue) -> EvidenceCoverageIssue:
    return EvidenceCoverageIssue(
        source=issue.source,
        endpoint=issue.endpoint,
        code=issue.code,
        message=issue.message,
        status_code=issue.status_code,
    )


def fetch_source_bundles(
    tickers: list[str] | tuple[str, ...],
    massive_client: MassiveEvidenceSource,
    sec_client: SecEvidenceSource,
    *,
    as_of: date,
    settings: EvidenceSettings = DEFAULT_EVIDENCE_SETTINGS,
) -> tuple[TickerSourceBundle, ...]:
    """Fetch raw provider payloads without performing any evidence derivation."""

    _validate_settings(settings)
    price_start = as_of - timedelta(days=settings.price_history_days)
    bundles: list[TickerSourceBundle] = []
    seen: set[str] = set()
    for raw_ticker in tickers:
        ticker = raw_ticker.upper()
        if not ticker:
            raise ValueError("ticker must be non-empty")
        if ticker in seen:
            raise ValueError(f"duplicate ticker {ticker}")
        seen.add(ticker)
        massive = massive_client.fetch_ticker_data(
            ticker,
            price_start,
            as_of,
            news_limit=settings.news_items,
            filing_limit=settings.filing_items,
            insider_limit=settings.insider_items,
        )
        coverage = [_coverage_from_massive(issue) for issue in massive.coverage_issues]
        details = massive.details or {}
        cik = details.get("cik")
        companyfacts: dict[str, Any] | None = None
        submissions: dict[str, Any] | None = None
        if isinstance(cik, str | int) and str(cik).strip():
            try:
                companyfacts = sec_client.get_company_facts(cik)
            except (SecError, ValueError) as exc:
                coverage.append(
                    EvidenceCoverageIssue(
                        source="sec",
                        endpoint="companyfacts",
                        code="request_failed",
                        message=str(exc),
                    )
                )
            try:
                submissions = sec_client.get_submissions(cik)
            except (SecError, ValueError) as exc:
                coverage.append(
                    EvidenceCoverageIssue(
                        source="sec",
                        endpoint="submissions",
                        code="request_failed",
                        message=str(exc),
                    )
                )
        else:
            coverage.append(
                EvidenceCoverageIssue(
                    source="sec",
                    endpoint="companyfacts/submissions",
                    code="missing_cik",
                    message="ticker details did not provide an SEC CIK",
                )
            )
        bundles.append(
            TickerSourceBundle(
                ticker=ticker,
                as_of=as_of,
                price_start=price_start,
                massive=massive,
                sec_companyfacts=companyfacts,
                sec_submissions=submissions,
                coverage_issues=tuple(coverage),
            )
        )
    return tuple(bundles)


def extract_facts(
    companyfacts: dict[str, Any] | None,
    aliases: tuple[str, ...],
    as_of: date,
) -> tuple[FactPoint, ...]:
    """Select XBRL facts with deterministic alias and amendment precedence."""

    if not companyfacts:
        return ()
    facts_root = companyfacts.get("facts")
    if not isinstance(facts_root, dict):
        return ()
    gaap = facts_root.get("us-gaap")
    if not isinstance(gaap, dict):
        return ()

    selected: dict[
        tuple[date | None, date, str | None],
        tuple[int, FactPoint],
    ] = {}
    for priority, tag in enumerate(aliases):
        concept = gaap.get(tag)
        if not isinstance(concept, dict):
            continue
        units = concept.get("units")
        if not isinstance(units, dict):
            continue
        raw_points = units.get("USD")
        if not isinstance(raw_points, list):
            continue
        for raw in raw_points:
            if not isinstance(raw, dict):
                continue
            value = _number(raw.get("val"))
            end = _parse_date(raw.get("end"))
            filed = _parse_date(raw.get("filed"))
            form = str(raw.get("form", ""))
            if (
                value is None
                or end is None
                or filed is None
                or filed > as_of
                or end > as_of
                or form not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}
            ):
                continue
            start = _parse_date(raw.get("start"))
            fy_raw = raw.get("fy")
            fiscal_year = int(fy_raw) if isinstance(fy_raw, int) and not isinstance(fy_raw, bool) else None
            fiscal_period_raw = raw.get("fp")
            fiscal_period = str(fiscal_period_raw) if isinstance(fiscal_period_raw, str) else None
            accession = raw.get("accn")
            point = FactPoint(
                tag=tag,
                value=value,
                start=start,
                end=end,
                filed=filed,
                form=form,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                accession_number=str(accession) if isinstance(accession, str) else None,
            )
            key = (start, end, fiscal_period)
            current = selected.get(key)
            if (
                current is None
                or priority < current[0]
                or (priority == current[0] and filed > current[1].filed)
            ):
                selected[key] = (priority, point)
    return tuple(
        point
        for _, point in sorted(
            selected.values(),
            key=lambda pair: (
                pair[1].end,
                pair[1].start or date.min,
                pair[1].filed,
                pair[1].tag,
            ),
        )
    )


def recent_submissions(
    submissions: dict[str, Any] | None,
    as_of: date,
    *,
    limit: int = 10,
) -> tuple[dict[str, Any], ...]:
    """Flatten recent submissions and keep only the newest amendment per period."""

    if not submissions:
        return ()
    filings = submissions.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        return ()
    forms = recent.get("form")
    if not isinstance(forms, list):
        return ()
    fields = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
    )
    rows: list[dict[str, Any]] = []
    for index in range(len(forms)):
        row = {
            field: values[index] if isinstance(values, list) and index < len(values) else None
            for field in fields
            if (values := recent.get(field)) is not None
        }
        filing_date = _parse_date(row.get("filingDate"))
        form = str(row.get("form", ""))
        if (
            filing_date is None
            or filing_date > as_of
            or form
            not in {
                "10-K",
                "10-K/A",
                "10-Q",
                "10-Q/A",
                "8-K",
                "8-K/A",
            }
        ):
            continue
        rows.append(row)

    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        base_form = str(row.get("form", "")).removesuffix("/A")
        report_date = str(row.get("reportDate") or row.get("accessionNumber") or "")
        key = (base_form, report_date)
        current = deduplicated.get(key)
        if current is None or str(row.get("filingDate", "")) > str(current.get("filingDate", "")):
            deduplicated[key] = row
    ordered = sorted(
        deduplicated.values(),
        key=lambda row: (
            str(row.get("filingDate", "")),
            str(row.get("accessionNumber", "")),
        ),
        reverse=True,
    )
    return tuple(ordered[:limit])


@dataclass
class _DraftPack:
    ticker: str
    as_of: date
    items: list[EvidenceItem]
    bar_count: int
    substantive: set[str]
    coverage: tuple[EvidenceCoverageIssue, ...]
    percentile_locations: dict[str, tuple[int, float]]


def _bar_date(row: dict[str, Any]) -> date | None:
    direct = _parse_date(row.get("date"))
    if direct is not None:
        return direct
    timestamp = _number(row.get("t"))
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp / 1000, UTC).date()


def _prepare_bars(rows: tuple[dict[str, Any], ...], as_of: date) -> list[tuple[date, float, float]]:
    by_date: dict[date, tuple[float, float]] = {}
    for row in rows:
        day = _bar_date(row)
        close = _number(row.get("c") if "c" in row else row.get("close"))
        volume = _number(row.get("v") if "v" in row else row.get("volume"))
        if day is None or day > as_of or close is None or volume is None or close <= 0 or volume < 0:
            continue
        by_date[day] = (close, volume)
    return [(day, by_date[day][0], by_date[day][1]) for day in sorted(by_date)]


def _drawdown(closes: list[float]) -> float:
    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        worst = min(worst, close / peak - 1.0)
    return worst


def _add_price_evidence(
    draft: _DraftPack,
    bars: list[tuple[date, float, float]],
) -> None:
    if not bars:
        return
    closes = [row[1] for row in bars]
    metrics: dict[str, float] = {}
    for sessions, name in (
        (20, "return_1m"),
        (60, "return_3m"),
        (252, "return_12m"),
    ):
        if len(closes) > sessions:
            metrics[name] = closes[-1] / closes[-sessions - 1] - 1.0
    if len(closes) > 1:
        log_returns = [math.log(current / prior) for prior, current in pairwise(closes)]
        metrics["annualized_volatility"] = statistics.pstdev(log_returns) * math.sqrt(252)
        metrics["max_drawdown_12m"] = _drawdown(closes[-252:])
    summary_bits = [
        f"{name.replace('_', ' ')} {value:+.1%}"
        for name, value in metrics.items()
        if name.startswith("return_")
    ]
    if "annualized_volatility" in metrics:
        summary_bits.append(f"annualized volatility {metrics['annualized_volatility']:.1%}")
    summary = (
        "Adjusted-price signals: " + "; ".join(summary_bits)
        if summary_bits
        else "Adjusted-price history is available but too short for trend metrics."
    )
    index = len(draft.items)
    draft.items.append(
        _item(
            draft.ticker,
            "price",
            bars[-1][0],
            bars[-1][0].isoformat(),
            summary,
            metrics=metrics,
            source="massive_adjusted_daily_bars",
        )
    )
    for name, value in metrics.items():
        draft.percentile_locations[name] = (index, value)
    dollar_volumes = [close * volume for _, close, volume in bars[-20:]]
    if dollar_volumes:
        draft.percentile_locations["liquidity"] = (
            index,
            float(statistics.median(dollar_volumes)),
        )


_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_ASSETS_TAGS = ("Assets",)
_LIABILITIES_TAGS = ("Liabilities",)
_OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForProceedsFromPropertyPlantAndEquipment",
)


def _annual(points: tuple[FactPoint, ...]) -> list[FactPoint]:
    by_end: dict[date, FactPoint] = {}
    for point in points:
        if point.form not in {"10-K", "10-K/A"}:
            continue
        if point.fiscal_period not in {None, "FY"}:
            continue
        current = by_end.get(point.end)
        point_start = point.start or point.end
        current_start = current.start if current is not None and current.start else point.end
        if (
            current is None
            or point.filed > current.filed
            or (point.filed == current.filed and point_start < current_start)
        ):
            by_end[point.end] = point
    return [by_end[end] for end in sorted(by_end)]


def _latest(points: tuple[FactPoint, ...]) -> FactPoint | None:
    return max(points, key=lambda point: (point.end, point.filed), default=None)


def _for_end(points: tuple[FactPoint, ...], end: date) -> FactPoint | None:
    matches = [point for point in points if point.end == end]
    return max(matches, key=lambda point: point.filed, default=None)


def _for_period(points: tuple[FactPoint, ...], reference: FactPoint) -> FactPoint | None:
    exact = [point for point in points if point.end == reference.end and point.start == reference.start]
    if exact:
        return max(exact, key=lambda point: point.filed)
    return _for_end(points, reference.end)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _add_fundamental_evidence(
    draft: _DraftPack,
    companyfacts: dict[str, Any] | None,
) -> None:
    revenue = extract_facts(companyfacts, _REVENUE_TAGS, draft.as_of)
    net_income = extract_facts(companyfacts, _NET_INCOME_TAGS, draft.as_of)
    operating_income = extract_facts(companyfacts, _OPERATING_INCOME_TAGS, draft.as_of)
    assets = extract_facts(companyfacts, _ASSETS_TAGS, draft.as_of)
    liabilities = extract_facts(companyfacts, _LIABILITIES_TAGS, draft.as_of)
    operating_cash_flow = extract_facts(companyfacts, _OPERATING_CASH_FLOW_TAGS, draft.as_of)
    capex = extract_facts(companyfacts, _CAPEX_TAGS, draft.as_of)

    metrics: dict[str, float] = {}
    used: list[FactPoint] = []
    annual_revenue = _annual(revenue)
    current_revenue = annual_revenue[-1] if annual_revenue else None
    if current_revenue is not None:
        used.append(current_revenue)
        if len(annual_revenue) >= 2:
            prior_revenue = annual_revenue[-2]
            growth = _safe_ratio(
                current_revenue.value - prior_revenue.value,
                abs(prior_revenue.value),
            )
            if growth is not None:
                metrics["revenue_growth_yoy"] = growth
                used.append(prior_revenue)
        current_net_income = _for_period(net_income, current_revenue)
        if current_net_income is not None:
            margin = _safe_ratio(current_net_income.value, current_revenue.value)
            if margin is not None:
                metrics["net_margin"] = margin
                used.append(current_net_income)
        current_operating = _for_period(operating_income, current_revenue)
        if current_operating is not None:
            margin = _safe_ratio(current_operating.value, current_revenue.value)
            if margin is not None:
                metrics["operating_margin"] = margin
                used.append(current_operating)
        current_cash_flow = _for_period(operating_cash_flow, current_revenue)
        if current_cash_flow is not None:
            cash_margin = _safe_ratio(current_cash_flow.value, current_revenue.value)
            if cash_margin is not None:
                metrics["operating_cash_flow_margin"] = cash_margin
                used.append(current_cash_flow)
            current_capex = _for_period(capex, current_revenue)
            if current_capex is not None:
                free_cash_flow_margin = _safe_ratio(
                    current_cash_flow.value - abs(current_capex.value),
                    current_revenue.value,
                )
                if free_cash_flow_margin is not None:
                    metrics["free_cash_flow_margin"] = free_cash_flow_margin
                    used.append(current_capex)

    latest_assets = _latest(assets)
    if latest_assets is not None:
        latest_liabilities = _for_end(liabilities, latest_assets.end)
        if latest_liabilities is not None:
            ratio = _safe_ratio(latest_liabilities.value, latest_assets.value)
            if ratio is not None:
                metrics["liabilities_to_assets"] = ratio
                used.extend((latest_assets, latest_liabilities))

    if not metrics or not used:
        return
    knowledge_date = max(point.filed for point in used)
    period_end = max(point.end for point in used)
    summary = "SEC-derived fundamentals: " + "; ".join(
        f"{name.replace('_', ' ')} {value:+.1%}"
        if "growth" in name
        else f"{name.replace('_', ' ')} {value:.1%}"
        for name, value in sorted(metrics.items())
    )
    index = len(draft.items)
    draft.items.append(
        _item(
            draft.ticker,
            "fundamentals",
            knowledge_date,
            f"{period_end.isoformat()}:{sorted(metrics)}",
            summary,
            metrics=metrics,
            source="sec_companyfacts",
        )
    )
    draft.substantive.add("fundamentals")
    for name, value in metrics.items():
        draft.percentile_locations[name] = (index, value)


def _record_date(row: dict[str, Any], *fields: str) -> date | None:
    for field_name in fields:
        parsed = _parse_date(row.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _source_key(row: dict[str, Any], *fields: str) -> str:
    values = [str(row.get(field_name, "")) for field_name in fields]
    if any(values):
        return ":".join(values)
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _add_news_evidence(
    draft: _DraftPack,
    rows: tuple[dict[str, Any], ...],
    limit: int,
) -> None:
    candidates: list[tuple[date, str, dict[str, Any]]] = []
    for row in rows:
        knowledge = _record_date(row, "published_utc", "published")
        if knowledge is None or knowledge > draft.as_of:
            continue
        key = _source_key(row, "id", "article_url", "title")
        candidates.append((knowledge, key, row))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for knowledge, key, row in candidates[:limit]:
        title = _sanitize_text(row.get("title"), limit=220)
        description = _sanitize_text(row.get("description"), limit=380)
        summary = title
        if description and description not in title:
            summary = f"{title}. {description}" if title else description
        if not summary:
            continue
        draft.items.append(
            _item(
                draft.ticker,
                "news",
                knowledge,
                key,
                summary,
                source="massive_news",
                source_url=str(row.get("article_url")) if row.get("article_url") else None,
            )
        )
        draft.substantive.add("news")


def _filing_candidates(
    bundle: TickerSourceBundle,
) -> list[tuple[date, str, str, str, str | None]]:
    candidates: list[tuple[date, str, str, str, str | None]] = []
    for row in bundle.massive.ten_k_sections:
        knowledge = _record_date(row, "filing_date")
        text = row.get("text")
        if knowledge is None or knowledge > bundle.as_of or not text:
            continue
        key = _source_key(row, "accession_number", "filing_url", "section", "period_end")
        section = str(row.get("section") or "section").replace("_", " ")
        candidates.append(
            (
                knowledge,
                key,
                f"10-K {section}: {_sanitize_text(text)}",
                "massive_10k_sections",
                str(row.get("filing_url")) if row.get("filing_url") else None,
            )
        )
    for row in bundle.massive.eight_k_text:
        knowledge = _record_date(row, "filing_date")
        text = row.get("items_text")
        if knowledge is None or knowledge > bundle.as_of or not text:
            continue
        key = _source_key(row, "accession_number", "filing_url")
        candidates.append(
            (
                knowledge,
                key,
                f"8-K filing: {_sanitize_text(text)}",
                "massive_8k_text",
                str(row.get("filing_url")) if row.get("filing_url") else None,
            )
        )
    for row in bundle.massive.eight_k_disclosures:
        knowledge = _record_date(row, "filing_date")
        if knowledge is None or knowledge > bundle.as_of:
            continue
        categories = [
            str(row.get(name))
            for name in (
                "primary_category",
                "secondary_category",
                "tertiary_category",
                "primary",
                "secondary",
                "tertiary",
            )
            if row.get(name)
        ]
        excerpt = row.get("text") or row.get("excerpt") or row.get("supporting_text") or ""
        summary = "8-K disclosure"
        if categories:
            summary += f" ({' / '.join(dict.fromkeys(categories))})"
        if excerpt:
            summary += f": {_sanitize_text(excerpt)}"
        key = _source_key(
            row,
            "accession_number",
            "disclosure_id",
            "primary_category",
            "secondary_category",
        )
        candidates.append(
            (
                knowledge,
                key,
                summary,
                "massive_8k_disclosures",
                str(row.get("filing_url")) if row.get("filing_url") else None,
            )
        )
    for row in recent_submissions(bundle.sec_submissions, bundle.as_of):
        knowledge = _record_date(row, "filingDate")
        if knowledge is None:
            continue
        form = str(row.get("form"))
        report_date = str(row.get("reportDate") or "unspecified period")
        key = _source_key(row, "accessionNumber", "form", "reportDate")
        candidates.append(
            (
                knowledge,
                key,
                f"SEC {form} filed for reporting period {report_date}.",
                "sec_submissions",
                None,
            )
        )
    return candidates


def _add_filing_evidence(
    draft: _DraftPack,
    bundle: TickerSourceBundle,
    limit: int,
) -> None:
    candidates = _filing_candidates(bundle)
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    seen: set[str] = set()
    for knowledge, key, summary, source, url in candidates:
        if key in seen:
            continue
        seen.add(key)
        draft.items.append(
            _item(
                draft.ticker,
                "filing",
                knowledge,
                key,
                summary,
                source=source,
                source_url=url,
            )
        )
        if source != "sec_submissions":
            draft.substantive.add("filing")
        if len(seen) >= limit:
            break


def _add_insider_evidence(
    draft: _DraftPack,
    rows: tuple[dict[str, Any], ...],
    limit: int,
) -> None:
    candidates: list[tuple[date, str, dict[str, Any]]] = []
    for row in rows:
        knowledge = _record_date(row, "filing_date")
        if knowledge is None or knowledge > draft.as_of:
            continue
        key = _source_key(
            row,
            "accession_number",
            "owner_cik",
            "transaction_date",
            "transaction_code",
        )
        candidates.append((knowledge, key, row))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for knowledge, key, row in candidates[:limit]:
        code = str(row.get("transaction_code") or "").upper()
        action = {
            "P": "open-market purchase",
            "S": "sale",
            "A": "grant or award",
            "M": "option exercise or conversion",
        }.get(code, "ownership change")
        owner = _sanitize_text(
            row.get("owner_name") or row.get("reporting_owner_name") or "corporate insider",
            limit=100,
        )
        transaction_date = str(row.get("transaction_date") or "unspecified date")
        draft.items.append(
            _item(
                draft.ticker,
                "insider",
                knowledge,
                key,
                f"{owner} reported an insider {action} dated {transaction_date}.",
                source="massive_form4",
                source_url=str(row.get("filing_url")) if row.get("filing_url") else None,
            )
        )
        draft.substantive.add("insider")


def _add_short_interest(draft: _DraftPack, rows: tuple[dict[str, Any], ...]) -> None:
    candidates = [
        (knowledge, row)
        for row in rows
        if (knowledge := _record_date(row, "settlement_date")) is not None and knowledge <= draft.as_of
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return
    knowledge, latest = candidates[0]
    days_to_cover = _number(latest.get("days_to_cover"))
    metrics: dict[str, float] = {}
    if days_to_cover is not None:
        metrics["short_days_to_cover"] = days_to_cover
    if len(candidates) > 1:
        previous = _number(candidates[1][1].get("days_to_cover"))
        if days_to_cover is not None and previous is not None and previous != 0.0:
            metrics["short_days_to_cover_change"] = days_to_cover / previous - 1.0
    if not metrics:
        return
    index = len(draft.items)
    draft.items.append(
        _item(
            draft.ticker,
            "short_interest",
            knowledge,
            knowledge.isoformat(),
            "Short-interest signals: "
            + "; ".join(f"{name.replace('_', ' ')} {value:.2f}" for name, value in metrics.items()),
            metrics=metrics,
            source="massive_short_interest",
        )
    )
    for name, value in metrics.items():
        draft.percentile_locations[name] = (index, value)


def _add_dividend_evidence(
    draft: _DraftPack,
    rows: tuple[dict[str, Any], ...],
    latest_close: float | None,
) -> None:
    if latest_close is None or latest_close <= 0:
        return
    candidates = [
        (knowledge, row)
        for row in rows
        if (knowledge := _record_date(row, "declaration_date", "ex_dividend_date")) is not None
        and knowledge <= draft.as_of
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return
    knowledge, latest = candidates[0]
    amount = _number(
        latest.get("split_adjusted_cash_amount")
        if latest.get("split_adjusted_cash_amount") is not None
        else latest.get("cash_amount")
    )
    frequency = _number(latest.get("frequency"))
    if amount is None or frequency is None:
        return
    yield_value = amount * frequency / latest_close
    index = len(draft.items)
    draft.items.append(
        _item(
            draft.ticker,
            "dividend",
            knowledge,
            _source_key(latest, "id", "ex_dividend_date"),
            f"Indicated dividend yield is {yield_value:.2%} at the evidence date.",
            metrics={"dividend_yield": yield_value},
            source="massive_dividends",
        )
    )
    draft.percentile_locations["dividend_yield"] = (index, yield_value)


def _add_split_evidence(draft: _DraftPack, rows: tuple[dict[str, Any], ...]) -> None:
    candidates = [
        (knowledge, row)
        for row in rows
        if (knowledge := _record_date(row, "execution_date")) is not None and knowledge <= draft.as_of
    ]
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return
    knowledge, latest = candidates[0]
    split_from = _number(latest.get("split_from"))
    split_to = _number(latest.get("split_to"))
    if split_from is None or split_to is None or split_from == 0:
        return
    ratio = split_to / split_from
    draft.items.append(
        _item(
            draft.ticker,
            "corporate_action",
            knowledge,
            _source_key(latest, "id", "execution_date"),
            f"Most recent stock split changed each prior share into {ratio:g} shares.",
            metrics={"split_ratio": ratio},
            source="massive_splits",
        )
    )


def _draft_bundle(bundle: TickerSourceBundle, settings: EvidenceSettings) -> _DraftPack:
    if bundle.as_of < bundle.price_start:
        raise ValueError("source bundle price_start is after as_of")
    bars = _prepare_bars(bundle.massive.daily_bars, bundle.as_of)
    draft = _DraftPack(
        ticker=bundle.ticker,
        as_of=bundle.as_of,
        items=[],
        bar_count=len(bars),
        substantive=set(),
        coverage=bundle.coverage_issues,
        percentile_locations={},
    )
    _add_price_evidence(draft, bars)
    _add_fundamental_evidence(draft, bundle.sec_companyfacts)
    _add_news_evidence(draft, bundle.massive.news, settings.news_items)
    _add_filing_evidence(draft, bundle, settings.filing_items)
    _add_insider_evidence(draft, bundle.massive.form4, settings.insider_items)
    _add_short_interest(draft, bundle.massive.short_interest)
    _add_dividend_evidence(draft, bundle.massive.dividends, bars[-1][1] if bars else None)
    _add_split_evidence(draft, bundle.massive.splits)
    draft.items.sort(
        key=lambda item: (
            item.category,
            -item.knowledge_date.toordinal(),
            item.evidence_id,
        )
    )
    # Sorting moves items after their percentile locations were recorded.
    id_to_index = {item.evidence_id: index for index, item in enumerate(draft.items)}
    rebuilt: dict[str, tuple[int, float]] = {}
    for name, (_, value) in draft.percentile_locations.items():
        owner = next(
            (
                item
                for item in draft.items
                if name in item.metrics or (name == "liquidity" and item.category == "price")
            ),
            None,
        )
        if owner is not None:
            rebuilt[name] = (id_to_index[owner.evidence_id], value)
    draft.percentile_locations = rebuilt
    return draft


def _percentile(value: float, population: list[float]) -> float:
    if len(population) <= 1:
        return 0.5
    less = sum(candidate < value for candidate in population)
    equal = sum(candidate == value for candidate in population)
    return (less + (equal - 1) / 2) / (len(population) - 1)


def _apply_percentiles(drafts: list[_DraftPack]) -> None:
    populations: dict[str, list[float]] = {}
    for draft in drafts:
        for name, (_, value) in draft.percentile_locations.items():
            populations.setdefault(name, []).append(value)
    for draft in drafts:
        by_item: dict[int, dict[str, float]] = {}
        for name, (index, value) in draft.percentile_locations.items():
            by_item.setdefault(index, {})[name] = _percentile(value, populations[name])
        for index, values in by_item.items():
            draft.items[index] = replace(
                draft.items[index],
                percentiles=dict(sorted(values.items())),
            )


def _validate_settings(settings: EvidenceSettings) -> None:
    for name in (
        "price_history_days",
        "min_price_bars",
        "news_items",
        "filing_items",
        "insider_items",
        "evidence_char_budget",
    ):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")


_ITEM_PRIORITY = {
    "fundamentals": 0,
    "price": 1,
    "filing": 2,
    "insider": 3,
    "news": 4,
    "short_interest": 5,
    "dividend": 6,
    "corporate_action": 7,
}
_SUBSTANTIVE_PRIORITY = ("fundamentals", "filing", "news", "insider")


def _prompt_size(pack: EvidencePack) -> int:
    return len(
        json.dumps(
            pack.as_prompt_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )


def _canonical_items(items: list[EvidenceItem]) -> tuple[EvidenceItem, ...]:
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.category,
                -item.knowledge_date.toordinal(),
                item.evidence_id,
            ),
        )
    )


def _trim_pack(pack: EvidencePack, budget: int) -> EvidencePack:
    if _prompt_size(pack) <= budget:
        return pack

    ordered = sorted(
        pack.items,
        key=lambda item: (
            _ITEM_PRIORITY.get(item.category, 99),
            -item.knowledge_date.toordinal(),
            item.evidence_id,
        ),
    )
    required_ids: set[str] = set()
    if pack.eligible:
        price = next((item for item in ordered if item.category == "price"), None)
        if price is None:
            raise ValueError(f"eligible pack {pack.ticker} has no price evidence")
        required_ids.add(price.evidence_id)
        substantive = next(
            (item for category in _SUBSTANTIVE_PRIORITY for item in ordered if item.category == category),
            None,
        )
        if substantive is None:
            raise ValueError(f"eligible pack {pack.ticker} has no substantive evidence")
        required_ids.add(substantive.evidence_id)

    selected = [item for item in ordered if item.evidence_id in required_ids]

    def candidate_pack(items: list[EvidenceItem]) -> EvidencePack:
        return replace(pack, items=_canonical_items(items))

    required_pack = candidate_pack(selected)
    if _prompt_size(required_pack) > budget and selected:
        compacted = selected
        for summary_limit in (320, 160, 80, 32):
            compacted = [
                replace(
                    item,
                    summary=_sanitize_text(item.summary, limit=summary_limit),
                    source_url=None,
                )
                for item in selected
            ]
            required_pack = candidate_pack(compacted)
            if _prompt_size(required_pack) <= budget:
                selected = compacted
                break
        else:
            raise ValueError(
                f"evidence_char_budget {budget} is too small for required "
                f"{pack.ticker} price and substantive evidence"
            )
    elif not selected:
        empty = candidate_pack([])
        if _prompt_size(empty) > budget:
            raise ValueError(f"evidence_char_budget {budget} is too small for pack metadata")

    selected_ids = {item.evidence_id for item in selected}
    for item in ordered:
        if item.evidence_id in selected_ids:
            continue
        trial = candidate_pack([*selected, item])
        if _prompt_size(trial) <= budget:
            selected.append(item)
            selected_ids.add(item.evidence_id)
    result = candidate_pack(selected)
    if _prompt_size(result) > budget:
        raise AssertionError("evidence trimming failed to meet its character budget")
    return result


def build_evidence_packs_from_sources(
    bundles: list[TickerSourceBundle] | tuple[TickerSourceBundle, ...],
    *,
    as_of: date,
    settings: EvidenceSettings = DEFAULT_EVIDENCE_SETTINGS,
) -> tuple[EvidencePack, ...]:
    """Deterministically derive prompt-safe packs from checkpointed raw inputs."""

    _validate_settings(settings)
    seen: set[str] = set()
    drafts: list[_DraftPack] = []
    for bundle in bundles:
        if bundle.as_of != as_of:
            raise ValueError(f"source bundle {bundle.ticker} as_of does not match build as_of")
        if bundle.ticker in seen:
            raise ValueError(f"duplicate source bundle for {bundle.ticker}")
        seen.add(bundle.ticker)
        drafts.append(_draft_bundle(bundle, settings))
    _apply_percentiles(drafts)

    packs: list[EvidencePack] = []
    for draft in drafts:
        substantive = tuple(sorted(draft.substantive))
        eligible = draft.bar_count >= settings.min_price_bars and bool(substantive)
        if draft.bar_count < settings.min_price_bars:
            exclusion = f"only {draft.bar_count} adjusted daily bars; {settings.min_price_bars} required"
        elif not substantive:
            exclusion = "no substantive fundamentals, filing, news, or insider evidence"
        else:
            exclusion = None
        packs.append(
            _trim_pack(
                EvidencePack(
                    ticker=draft.ticker,
                    as_of=as_of,
                    eligible=eligible,
                    exclusion_reason=exclusion,
                    bar_count=draft.bar_count,
                    substantive_categories=substantive,
                    items=tuple(draft.items),
                    coverage_issues=draft.coverage,
                ),
                settings.evidence_char_budget,
            )
        )
    return tuple(packs)


def build_evidence_packs(
    tickers: list[str] | tuple[str, ...],
    massive_client: MassiveEvidenceSource,
    sec_client: SecEvidenceSource,
    *,
    as_of: date,
    settings: EvidenceSettings = DEFAULT_EVIDENCE_SETTINGS,
) -> tuple[EvidencePack, ...]:
    """Convenience wrapper for callers that do not checkpoint raw responses."""

    bundles = fetch_source_bundles(
        tickers,
        massive_client,
        sec_client,
        as_of=as_of,
        settings=settings,
    )
    return build_evidence_packs_from_sources(bundles, as_of=as_of, settings=settings)
