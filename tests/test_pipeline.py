from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockrank.config import write_toml
from stockrank.credentials import CredentialError, CredentialStore
from stockrank.llm import RaceModelError, RaceResult
from stockrank.massive import MassiveResult, MassiveTickerData
from stockrank.pipeline import execute_rank
from stockrank.pipeline import test_model as check_model
from stockrank.races import replacement_group, schedule
from stockrank.runs import RunError, RunStore, atomic_write_json

TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
NOW = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


def _project(root: Path) -> None:
    write_toml(
        root / "stockrank.toml",
        {
            "sources": {
                "massive": {
                    "concurrency": 1,
                },
                "sec": {},
            },
            "data": {
                "price_history_days": 60,
                "news_items": 2,
                "filing_items": 1,
                "insider_items": 1,
                "min_price_bars": 2,
                "evidence_char_budget": 4000,
            },
            "defaults": {
                "model": "fake",
                "mode": "best-bet",
                "profile": "low",
                "seed": 37,
            },
            "models": {
                "fake": {
                    "base_url": "https://model.invalid",
                    "model": "fake-model",
                    "concurrency": 1,
                    "max_tokens": 500,
                }
            },
        },
    )
    write_toml(
        root / "modes" / "best-bet.toml",
        {
            "name": "best-bet",
            "rank_1_meaning": "the strongest evidence-adjusted bet",
            "prompt": "Rank the supplied stocks as evidence-adjusted bets.",
        },
    )
    write_toml(
        root / "universes" / "six.toml",
        {"name": "Six", "kind": "static", "tickers": list(TICKERS)},
    )


class FakeMassive:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []
        self.fetch_calls: list[str] = []

    def get_ticker_details(
        self,
        ticker: str,
        as_of: date | None = None,
    ) -> MassiveResult:
        del as_of
        self.detail_calls.append(ticker)
        return MassiveResult(
            value={
                "ticker": ticker,
                "active": True,
                "cik": str(TICKERS.index(ticker) + 1),
            }
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
        del start, news_limit, filing_limit, insider_limit
        self.fetch_calls.append(ticker)
        bars = tuple(
            {
                "date": (end - timedelta(days=offset)).isoformat(),
                "c": 100.0 + TICKERS.index(ticker) + offset,
                "v": 1_000.0 + offset,
            }
            for offset in (2, 1, 0)
        )
        return MassiveTickerData(
            ticker=ticker,
            details={
                "ticker": ticker,
                "active": True,
                "cik": str(TICKERS.index(ticker) + 1),
            },
            daily_bars=bars,
            news=(
                {
                    "id": f"{ticker}-news",
                    "published_utc": f"{end.isoformat()}T08:00:00Z",
                    "title": f"{ticker} reports fresh demand evidence",
                },
            ),
            ten_k_sections=(),
            eight_k_text=(),
            eight_k_disclosures=(),
            form4=(),
            short_interest=(),
            dividends=(),
            splits=(),
            coverage_issues=(),
        )


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


class FakeSec:
    def __init__(self) -> None:
        self.company_fact_calls: list[str] = []
        self.submission_calls: list[str] = []

    def get_company_facts(self, cik: str | int) -> dict[str, object]:
        self.company_fact_calls.append(str(cik))
        return {}

    def get_submissions(self, cik: str | int) -> dict[str, object]:
        self.submission_calls.append(str(cik))
        return {}


class ScriptedJudge:
    def __init__(self, *, fail_calls: Collection[int] = ()) -> None:
        self.fail_calls = set(fail_calls)
        self.calls: list[tuple[str, ...]] = []

    def judge(
        self,
        tickers: Sequence[str],
        evidence_by_ticker: Mapping[str, object],
        mode_prompt: str,
        allowed_evidence_ids: Mapping[str, Collection[str]] | None = None,
    ) -> RaceResult:
        call_index = len(self.calls)
        race = tuple(tickers)
        self.calls.append(race)
        assert set(evidence_by_ticker) == set(race)
        assert mode_prompt == "Rank the supplied stocks as evidence-adjusted bets."
        assert allowed_evidence_ids is not None
        if call_index in self.fail_calls:
            raise RaceModelError(f"scripted failure {call_index}")
        return RaceResult.model_validate(
            {
                "ranking": list(race),
                "judgments": [
                    {
                        "ticker": ticker,
                        "load_bearing_question": f"What changes the case for {ticker}?",
                        "thesis": f"{ticker} has the strongest supplied evidence here.",
                        "evidence_ids": [next(iter(allowed_evidence_ids[ticker]))],
                        "falsifier": f"A reversal in {ticker}'s cited evidence.",
                        "conviction": 0.6,
                    }
                    for ticker in race
                ],
            },
            strict=True,
        )


def _execute(
    root: Path,
    massive: FakeMassive,
    sec: FakeSec,
    judge: ScriptedJudge,
    *,
    resume_id: str | None = None,
) -> str:
    return execute_rank(
        root,
        universe_name=None if resume_id else "six",
        resume_id=resume_id,
        now=NOW,
        massive_client=massive,  # type: ignore[arg-type]
        sec_client=sec,  # type: ignore[arg-type]
        judge=judge,
        bootstrap_samples=5,
    )


def test_execute_rank_creates_complete_auditable_artifacts(tmp_path: Path) -> None:
    _project(tmp_path)
    massive = FakeMassive()
    sec = FakeSec()
    judge = ScriptedJudge()

    run_id = _execute(tmp_path, massive, sec, judge)
    paths = RunStore(tmp_path).paths(run_id)
    manifest = RunStore(tmp_path).load(run_id)

    assert manifest.status == "completed"
    assert manifest.universe["tickers"] == list(TICKERS)
    assert paths.universe.is_file()
    assert paths.schedule.is_file()
    assert paths.ranking_csv.is_file()
    assert paths.ranking_json.is_file()
    assert paths.report.is_file()
    assert len(list(paths.raw.glob("*/*/bundle.json"))) == len(TICKERS)
    assert len(list(paths.evidence_dir.glob("*.json"))) == len(TICKERS)
    assert len(list(paths.races_dir.glob("race-*/request.json"))) == 4
    assert len(list(paths.races_dir.glob("race-*/response.json"))) == 4
    assert len(judge.calls) == 4
    assert massive.detail_calls == list(TICKERS)
    assert massive.fetch_calls == list(TICKERS)
    assert sec.company_fact_calls == [str(index) for index in range(1, 7)]
    ranking = json.loads(paths.ranking_json.read_text(encoding="utf-8"))
    assert len(ranking["ranking"]) == len(TICKERS)
    assert ranking["method"]["bootstrap_samples"] == 5
    assert "# Stock ranking:" in paths.report.read_text(encoding="utf-8")


def test_new_run_preflights_every_credential_before_network_or_artifacts(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    credentials = CredentialStore(MemoryKeyring())

    with pytest.raises(CredentialError, match="Massive API key"):
        execute_rank(
            tmp_path,
            universe_name="six",
            now=NOW,
            credential_store=credentials,
        )

    credentials.set_massive_key("present")
    with pytest.raises(CredentialError, match="LLM profile 'fake'"):
        execute_rank(
            tmp_path,
            universe_name="six",
            now=NOW,
            credential_store=credentials,
        )

    assert RunStore(tmp_path).list() == []


def test_resume_skips_valid_source_evidence_and_race_artifacts(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    massive = FakeMassive()
    sec = FakeSec()
    initial_judge = ScriptedJudge(fail_calls={0, 4, 5})

    with pytest.raises(RaceModelError, match="failed after 3 attempts"):
        _execute(tmp_path, massive, sec, initial_judge)

    run_id = RunStore(tmp_path).list()[0].id
    paths = RunStore(tmp_path).paths(run_id)
    assert RunStore(tmp_path).load(run_id).status == "failed"
    assert len(list(paths.races_dir.glob("race-*/response.json"))) == 3
    fetch_count = len(massive.fetch_calls)
    sec_count = len(sec.company_fact_calls) + len(sec.submission_calls)
    evidence_before = {path.name: path.read_bytes() for path in paths.evidence_dir.glob("*.json")}
    valid_races_before = {
        path.parent.name: path.read_bytes() for path in paths.races_dir.glob("race-*/response.json")
    }
    orphaned_response = min(paths.races_dir.glob("race-*/response.json"))
    orphaned_request = orphaned_response.parent / "request.json"
    orphaned_request.unlink()
    resumed_judge = ScriptedJudge()

    assert (
        _execute(
            tmp_path,
            massive,
            sec,
            resumed_judge,
            resume_id=run_id,
        )
        == run_id
    )

    assert len(massive.fetch_calls) == fetch_count
    assert len(sec.company_fact_calls) + len(sec.submission_calls) == sec_count
    assert len(resumed_judge.calls) == 2
    assert orphaned_request.is_file()
    assert evidence_before == {path.name: path.read_bytes() for path in paths.evidence_dir.glob("*.json")}
    assert (
        valid_races_before.items()
        <= {
            path.parent.name: path.read_bytes() for path in paths.races_dir.glob("race-*/response.json")
        }.items()
    )
    manifest = RunStore(tmp_path).load(run_id)
    assert manifest.status == "completed"
    assert [event.event for event in manifest.lifecycle] == [
        "created",
        "failed",
        "resumed",
        "completed",
    ]


def test_failed_race_uses_seeded_deterministic_replacement_group(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    massive = FakeMassive()
    sec = FakeSec()
    judge = ScriptedJudge(fail_calls={0})

    run_id = _execute(tmp_path, massive, sec, judge)
    paths = RunStore(tmp_path).paths(run_id)
    expected_schedule = schedule("low", TICKERS, 37)
    expected_retry = replacement_group(
        expected_schedule.groups[0],
        expected_schedule.seed,
        1,
    )

    assert len(judge.calls) == 5
    assert judge.calls[-1] == expected_retry.tickers
    retry_request = json.loads(paths.race_request("race-0000-r1").read_text(encoding="utf-8"))
    assert retry_request["tickers"] == list(expected_retry.tickers)
    assert retry_request["replacement_for"] == 0
    assert retry_request["replacement_attempt"] == 1
    assert paths.race_dir("race-0000").joinpath("error.json").is_file()
    assert paths.race_response("race-0000-r1").is_file()


def test_resume_rejects_parseable_evidence_tampering(tmp_path: Path) -> None:
    _project(tmp_path)
    massive = FakeMassive()
    sec = FakeSec()
    with pytest.raises(RaceModelError):
        _execute(
            tmp_path,
            massive,
            sec,
            ScriptedJudge(fail_calls={0, 4, 5}),
        )
    run_id = RunStore(tmp_path).list()[0].id
    evidence_path = RunStore(tmp_path).paths(run_id).evidence("AAA")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["evidence"][0]["summary"] = "tampered but parseable"
    atomic_write_json(evidence_path, evidence)
    resumed_judge = ScriptedJudge()

    with pytest.raises(RunError, match="does not match its frozen source"):
        _execute(
            tmp_path,
            massive,
            sec,
            resumed_judge,
            resume_id=run_id,
        )

    assert resumed_judge.calls == []
    assert RunStore(tmp_path).load(run_id).status == "failed"


def test_model_uses_generic_openai_factory_without_a_completion(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    write_toml(
        tmp_path / "stockrank.toml",
        {
            "sources": {
                "massive": {},
                "sec": {},
            },
            "defaults": {"model": "deepseek"},
            "models": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-pro",
                    "timeout_seconds": 45.0,
                    "max_retries": 4,
                }
            },
        },
    )
    credentials = CredentialStore(MemoryKeyring())
    credentials.set_llm_key("deepseek", "secret")
    constructor_calls: list[dict[str, object]] = []
    list_calls = 0

    class Models:
        def list(self):
            nonlocal list_calls
            list_calls += 1
            return SimpleNamespace(data=[SimpleNamespace(id="deepseek-v4-pro")])

    def factory(**kwargs):
        constructor_calls.append(kwargs)
        return SimpleNamespace(models=Models())

    assert (
        check_model(
            tmp_path,
            "deepseek",
            client_factory=factory,
            credential_store=credentials,
        )
        == "deepseek"
    )
    assert constructor_calls == [
        {
            "api_key": "secret",
            "base_url": "https://api.deepseek.com",
            "timeout": 45.0,
            "max_retries": 4,
        }
    ]
    assert list_calls == 1
