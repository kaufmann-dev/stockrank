from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from stockrank.llm import (
    OpenAIRaceJudge,
    RaceModelError,
    RaceRequest,
    RaceValidationError,
    validate_race_result,
)


@dataclass(frozen=True)
class Profile:
    name: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 17.0
    max_retries: int = 3
    concurrency: int = 2
    max_tokens: int = 800
    reasoning_effort: str | None = "high"
    extra_body: dict[str, object] | None = None


def race_json(
    tickers: tuple[str, ...] = ("AAA", "BBB"),
    *,
    evidence_id: str = "AAA-price",
) -> str:
    return json.dumps(
        {
            "ranking": list(tickers),
            "judgments": [
                {
                    "ticker": ticker,
                    "load_bearing_question": f"What matters for {ticker}?",
                    "thesis": f"{ticker} thesis",
                    "evidence_ids": [evidence_id if ticker == "AAA" else f"{ticker}-price"],
                    "falsifier": f"{ticker} falsifier",
                    "conviction": 0.7,
                }
                for ticker in tickers
            ],
        }
    )


class FakeCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            content = self.contents.pop(0)
        message = SimpleNamespace(
            content=content,
            reasoning_content="this must never be parsed",
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(contents))


class RaisingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        raise self.error


@dataclass(frozen=True)
class EvidencePack:
    ticker: str

    @property
    def allowed_evidence_ids(self) -> tuple[str, ...]:
        return (f"{self.ticker}-price",)

    def as_prompt_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "evidence": [{"id": f"{self.ticker}-price", "summary": "Prompt form"}],
        }


def evidence(tickers: tuple[str, ...] = ("AAA", "BBB")) -> dict[str, object]:
    return {ticker: {"items": [{"evidence_id": f"{ticker}-price", "value": 1.0}]} for ticker in tickers}


def allowed(tickers: tuple[str, ...] = ("AAA", "BBB")) -> dict[str, set[str]]:
    return {ticker: {f"{ticker}-price"} for ticker in tickers}


def test_deepseek_uses_generic_openai_client_configuration_and_message_content() -> None:
    created: list[dict[str, object]] = []
    fake = FakeClient([race_json()])

    def factory(**kwargs):
        created.append(kwargs)
        return fake

    profile = Profile(extra_body={"thinking": {"type": "disabled"}})
    judge = OpenAIRaceJudge(
        profile,
        client_factory=factory,
        environ={"DEEPSEEK_API_KEY": "secret"},
    )

    result = judge.judge(("AAA", "BBB"), evidence(), "Prefer the best bet.")

    assert result.ranking == ["AAA", "BBB"]
    assert created == [
        {
            "api_key": "secret",
            "base_url": "https://api.deepseek.com",
            "timeout": 17.0,
            "max_retries": 3,
        }
    ]
    request = fake.chat.completions.calls[0]
    assert request["model"] == "deepseek-chat"
    assert request["max_tokens"] == 800
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "exactly one JSON object" in request["messages"][0]["content"]
    assert "Prefer the best bet." in request["messages"][1]["content"]


def test_invalid_citation_gets_exactly_one_repair_request() -> None:
    fake = FakeClient(
        [
            race_json(evidence_id="invented"),
            race_json(),
        ]
    )
    judge = OpenAIRaceJudge(Profile(), client=fake)

    result = judge.judge(
        ("AAA", "BBB"),
        evidence(),
        "Rank them.",
        allowed(),
    )

    assert result.ranking == ["AAA", "BBB"]
    assert len(fake.chat.completions.calls) == 2
    repair_messages = fake.chat.completions.calls[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert "unknown evidence IDs" in repair_messages[-1]["content"]


def test_evidence_pack_interface_supplies_prompt_and_allowed_ids() -> None:
    fake = FakeClient([race_json()])
    judge = OpenAIRaceJudge(Profile(), client=fake)
    packs = {ticker: EvidencePack(ticker) for ticker in ("AAA", "BBB")}

    result = judge.judge(("AAA", "BBB"), packs, "Rank them.")

    assert result.ranking == ["AAA", "BBB"]
    user_prompt = fake.chat.completions.calls[0]["messages"][1]["content"]
    assert "Prompt form" in user_prompt


def test_invalid_second_response_stops_after_one_repair() -> None:
    fake = FakeClient(["not json", "still not json", race_json()])
    judge = OpenAIRaceJudge(Profile(), client=fake)

    with pytest.raises(RaceModelError, match="after one repair") as captured:
        judge.judge(("AAA", "BBB"), evidence(), "Rank them.", allowed())

    assert len(fake.chat.completions.calls) == 2
    assert captured.value.raw_responses == ("not json", "still not json")


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            race_json(("AAA", "CCC")),
            "exact permutation",
        ),
        (
            json.dumps(
                {
                    "ranking": ["AAA", "BBB"],
                    "judgments": [
                        {
                            "ticker": "AAA",
                            "load_bearing_question": "q",
                            "thesis": "t",
                            "evidence_ids": ["AAA-price"],
                            "falsifier": "f",
                            "conviction": 0.5,
                        },
                        {
                            "ticker": "AAA",
                            "load_bearing_question": "q",
                            "thesis": "t",
                            "evidence_ids": ["AAA-price"],
                            "falsifier": "f",
                            "conviction": 0.5,
                        },
                    ],
                }
            ),
            "exactly one entry",
        ),
    ],
)
def test_result_requires_exact_ticker_permutation_and_judgments(
    payload: str,
    message: str,
) -> None:
    with pytest.raises(RaceValidationError, match=message):
        validate_race_result(payload, ("AAA", "BBB"), allowed())


def test_strict_schema_rejects_extra_fields_and_string_conviction() -> None:
    payload = json.loads(race_json())
    payload["judgments"][0]["conviction"] = "0.7"
    payload["unexpected"] = True

    with pytest.raises(RaceValidationError, match="valid RaceResult"):
        validate_race_result(json.dumps(payload), ("AAA", "BBB"), allowed())


def test_judge_many_preserves_request_order() -> None:
    first = ("AAA", "BBB")
    second = ("CCC", "DDD")
    fake = FakeClient([race_json(first), race_json(second)])
    judge = OpenAIRaceJudge(Profile(), client=fake)
    requests = [
        RaceRequest(first, evidence(first), "Rank."),
        RaceRequest(second, evidence(second), "Rank."),
    ]

    results = judge.judge_many(requests)

    assert [result.ranking for result in results] == [list(first), list(second)]


def test_missing_api_key_is_reported_before_client_creation() -> None:
    with pytest.raises(RaceModelError, match="DEEPSEEK_API_KEY"):
        OpenAIRaceJudge(Profile(), environ={})


@pytest.mark.parametrize(
    "error",
    [
        APITimeoutError(request=httpx.Request("POST", "https://api.example.test")),
        RateLimitError(
            "rate limited",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "https://api.example.test"),
            ),
            body=None,
        ),
    ],
)
def test_exhausted_transport_errors_are_not_mistaken_for_json_failures(
    error: Exception,
) -> None:
    fake = RaisingClient(error)
    judge = OpenAIRaceJudge(Profile(), client=fake)

    with pytest.raises(type(error)):
        judge.judge(("AAA", "BBB"), evidence(), "Rank.")

    assert fake.calls == 1
