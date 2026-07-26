from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from stockrank.evidence import (
    EvidencePack,
    EvidenceSettings,
    TickerSourceBundle,
    build_evidence_packs_from_sources,
    extract_facts,
    recent_submissions,
)
from stockrank.massive import MassiveTickerData

AS_OF = date(2026, 7, 25)


def _bars(multiplier: float, count: int = 65) -> tuple[dict, ...]:
    start = AS_OF - timedelta(days=count - 1)
    return tuple(
        {
            "t": int(
                datetime.combine(
                    start + timedelta(days=index),
                    datetime.min.time(),
                    tzinfo=UTC,
                ).timestamp()
                * 1000
            ),
            "c": multiplier * (100 + index),
            "v": 1_000 + index,
        }
        for index in range(count)
    )


def _companyfacts(multiplier: float = 1.0) -> dict:
    def duration(prior: float, current: float, amended: float | None = None) -> list[dict]:
        rows = [
            {
                "start": "2024-01-01",
                "end": "2024-12-31",
                "val": prior * multiplier,
                "filed": "2025-02-01",
                "form": "10-K",
                "fy": 2024,
                "fp": "FY",
                "accn": "prior",
            },
            {
                "start": "2025-01-01",
                "end": "2025-12-31",
                "val": current * multiplier,
                "filed": "2026-02-01",
                "form": "10-K",
                "fy": 2025,
                "fp": "FY",
                "accn": "current",
            },
        ]
        if amended is not None:
            rows.append(
                {
                    **rows[-1],
                    "val": amended * multiplier,
                    "filed": "2026-03-01",
                    "form": "10-K/A",
                    "accn": "amended",
                }
            )
        return rows

    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": duration(100, 120, 125)}
                },
                "NetIncomeLoss": {"units": {"USD": duration(10, 15)}},
                "OperatingIncomeLoss": {"units": {"USD": duration(12, 20)}},
                "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": duration(14, 22)}},
                "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": duration(4, 5)}},
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-12-31",
                                "val": 200 * multiplier,
                                "filed": "2026-02-01",
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "assets",
                            }
                        ]
                    }
                },
                "Liabilities": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-12-31",
                                "val": 80 * multiplier,
                                "filed": "2026-02-01",
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                                "accn": "liabilities",
                            }
                        ]
                    }
                },
            }
        }
    }


def _submissions() -> dict:
    return {
        "filings": {
            "recent": {
                "accessionNumber": ["original", "amended", "quarter"],
                "filingDate": ["2026-02-01", "2026-03-01", "2026-05-01"],
                "reportDate": ["2025-12-31", "2025-12-31", "2026-03-31"],
                "form": ["10-K", "10-K/A", "10-Q"],
                "primaryDocument": ["a.htm", "a-amend.htm", "q.htm"],
            }
        }
    }


def _bundle(
    ticker: str,
    multiplier: float,
    *,
    bars: int = 65,
    with_substantive: bool = True,
) -> TickerSourceBundle:
    massive = MassiveTickerData(
        ticker=ticker,
        details={"ticker": ticker, "cik": "1"},
        daily_bars=_bars(multiplier, bars),
        news=(
            {
                "id": f"{ticker}-news",
                "published_utc": "2026-07-20T12:00:00Z",
                "title": f"{ticker} wins a $5 billion contract",
                "description": "Management expects durable demand.",
                "article_url": f"https://example.test/{ticker}",
            },
        )
        if with_substantive
        else (),
        ten_k_sections=(
            {
                "accession_number": f"{ticker}-10k",
                "filing_date": "2026-02-01",
                "period_end": "2025-12-31",
                "section": "risk_factors",
                "text": "Competition and execution failures could reduce demand.",
                "filing_url": f"https://example.test/{ticker}/10k",
            },
        )
        if with_substantive
        else (),
        eight_k_text=(),
        eight_k_disclosures=(),
        form4=(),
        short_interest=(
            {
                "settlement_date": "2026-07-15",
                "days_to_cover": 2.5,
            },
        ),
        dividends=(
            {
                "id": f"{ticker}-dividend",
                "declaration_date": "2026-07-01",
                "ex_dividend_date": "2026-07-15",
                "split_adjusted_cash_amount": 0.25,
                "frequency": 4,
            },
        ),
        splits=(),
        coverage_issues=(),
    )
    return TickerSourceBundle(
        ticker=ticker,
        as_of=AS_OF,
        price_start=AS_OF - timedelta(days=550),
        massive=massive,
        sec_companyfacts=_companyfacts(multiplier) if with_substantive else None,
        sec_submissions=_submissions() if with_substantive else None,
    )


def test_xbrl_alias_selection_amendment_dedup_and_submission_dedup() -> None:
    facts = extract_facts(
        _companyfacts(),
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
        ),
        AS_OF,
    )
    assert facts[-1].value == 125
    assert facts[-1].accession_number == "amended"

    submissions = recent_submissions(_submissions(), AS_OF)
    annual = [row for row in submissions if str(row["form"]).startswith("10-K")]
    assert len(annual) == 1
    assert annual[0]["accessionNumber"] == "amended"


def test_evidence_is_stable_prompt_safe_normalized_and_round_trippable() -> None:
    bundles = (_bundle("AAA", 1.0), _bundle("BBB", 2.0))
    packs = build_evidence_packs_from_sources(bundles, as_of=AS_OF, settings=EvidenceSettings())
    repeated = build_evidence_packs_from_sources(bundles, as_of=AS_OF, settings=EvidenceSettings())
    assert [pack.allowed_evidence_ids for pack in packs] == [pack.allowed_evidence_ids for pack in repeated]
    assert all(pack.eligible for pack in packs)
    assert all({"fundamentals", "filing", "news"} <= set(pack.substantive_categories) for pack in packs)

    first = packs[0]
    prompt = first.as_prompt_dict()
    encoded = str(prompt)
    assert "$5" not in encoded
    assert "125" not in encoded
    assert "200" not in encoded
    assert "[monetary amount omitted]" in encoded
    fundamental = next(item for item in first.items if item.category == "fundamentals")
    assert fundamental.metrics["revenue_growth_yoy"] == pytest.approx(0.25)
    assert "revenue_growth_yoy" in fundamental.percentiles

    price_percentiles = [
        next(item for item in pack.items if item.category == "price").percentiles for pack in packs
    ]
    assert price_percentiles[0]["liquidity"] == 0.0
    assert price_percentiles[1]["liquidity"] == 1.0

    restored = EvidencePack.from_dict(first.as_dict())
    assert restored == first
    raw_restored = TickerSourceBundle.from_dict(bundles[0].as_dict())
    assert raw_restored.as_dict() == bundles[0].as_dict()


def test_eligibility_requires_60_bars_and_a_substantive_category() -> None:
    settings = EvidenceSettings(min_price_bars=60)
    short = build_evidence_packs_from_sources(
        (_bundle("SHORT", 1.0, bars=59),),
        as_of=AS_OF,
        settings=settings,
    )[0]
    assert not short.eligible
    assert "59 adjusted daily bars" in str(short.exclusion_reason)

    price_only = build_evidence_packs_from_sources(
        (_bundle("PRICE", 1.0, with_substantive=False),),
        as_of=AS_OF,
        settings=settings,
    )[0]
    assert not price_only.eligible
    assert "no substantive" in str(price_only.exclusion_reason)


def test_resume_parsers_reject_future_or_duplicate_evidence() -> None:
    pack = build_evidence_packs_from_sources((_bundle("AAA", 1.0),), as_of=AS_OF)[0].as_prompt_dict()
    pack["evidence"].append(dict(pack["evidence"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        EvidencePack.from_dict(pack)

    pack["evidence"].pop()
    pack["evidence"][0]["knowledge_date"] = "2030-01-01"
    with pytest.raises(ValueError, match="future"):
        EvidencePack.from_dict(pack)


def test_pack_budget_is_deterministic_and_preserves_required_evidence() -> None:
    settings = EvidenceSettings(evidence_char_budget=1_800)
    pack = build_evidence_packs_from_sources((_bundle("AAA", 1.0),), as_of=AS_OF, settings=settings)[0]
    encoded = json.dumps(
        pack.as_prompt_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert len(encoded) <= 1_800
    assert any(item.category == "price" for item in pack.items)
    assert any(item.category in {"fundamentals", "filing", "news", "insider"} for item in pack.items)
    assert (
        pack.allowed_evidence_ids
        == build_evidence_packs_from_sources((_bundle("AAA", 1.0),), as_of=AS_OF, settings=settings)[
            0
        ].allowed_evidence_ids
    )

    with pytest.raises(ValueError, match="positive"):
        build_evidence_packs_from_sources(
            (_bundle("AAA", 1.0),),
            as_of=AS_OF,
            settings=EvidenceSettings(evidence_char_budget=0),
        )
