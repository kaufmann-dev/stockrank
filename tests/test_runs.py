from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stockrank.config import ENGINE_VERSION
from stockrank.runs import (
    IncompatibleRunError,
    RunError,
    RunStore,
    SourceArtifact,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_text,
    atomic_write_toml,
    read_csv,
    read_json,
    read_toml,
)


def _create(store: RunStore, second: int = 0):
    return store.create(
        universe={"name": "liquid-50", "tickers": ["AAA", "BBB"]},
        mode={"name": "best-bet", "rank_1_meaning": "best forward bet"},
        model={"name": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
        settings={
            "massive": {"base_url": "https://api.massive.com", "concurrency": 4},
            "data": {"minimum_bars": 60},
        },
        profile="medium",
        seed=17,
        now=datetime(2026, 7, 26, 10, 11, second, 123456, tzinfo=UTC),
    )


def test_atomic_artifact_helpers_round_trip(tmp_path: Path) -> None:
    atomic_write_json(tmp_path / "nested" / "value.json", {"b": [2], "a": 1})
    atomic_write_toml(tmp_path / "value.toml", {"model": {"name": "deepseek"}})
    atomic_write_text(tmp_path / "report.md", "# Report\n")
    atomic_write_csv(
        tmp_path / "ranking.csv",
        ("rank", "ticker", "strength"),
        ({"rank": 1, "ticker": "AAA", "strength": 2.5},),
    )

    assert read_json(tmp_path / "nested" / "value.json") == {"a": 1, "b": [2]}
    assert read_toml(tmp_path / "value.toml") == {"model": {"name": "deepseek"}}
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == "# Report\n"
    assert read_csv(tmp_path / "ranking.csv", required_fields=("rank", "ticker")) == [
        {"rank": "1", "ticker": "AAA", "strength": "2.5"}
    ]


def test_store_creates_frozen_timestamp_run_and_selects_latest(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    older = _create(store, second=1)
    newer = _create(store, second=2)

    assert older.id == "20260726T101101123456Z"
    assert older.engine_version == ENGINE_VERSION
    assert older.status == "running"
    assert older.universe["tickers"] == ["AAA", "BBB"]
    assert older.settings["data"]["minimum_bars"] == 60
    assert store.paths(older.id).manifest.exists()
    assert read_json(store.paths(older.id).universe)["name"] == "liquid-50"
    assert [manifest.id for manifest in store.list()] == [newer.id, older.id]
    assert store.select().id == newer.id
    assert store.select(older.id).id == older.id

    with pytest.raises(RunError, match="invalid timestamp run id"):
        store.paths("../escape")


def test_resume_skips_only_parse_valid_expected_artifacts(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    manifest = _create(store)
    paths = store.paths(manifest.id)
    good_source = SourceArtifact("massive", "AAA", "details")
    missing_source = SourceArtifact("sec", "BBB", "companyfacts")
    atomic_write_json(paths.raw_source("massive", "AAA", "details"), {"ticker": "AAA"})
    atomic_write_json(paths.evidence("AAA"), {"ticker": "AAA", "eligible": True})
    atomic_write_text(paths.evidence("BBB"), "{broken")
    atomic_write_json(paths.race_response("race-001"), {"ranking": ["AAA", "BBB"]})
    atomic_write_json(paths.race_response("race-002"), {"ranking": ["AAA"]})

    resumed = store.resume(
        manifest.id,
        sources=(good_source, missing_source),
        evidence_tickers=("AAA", "BBB"),
        race_ids=("race-001", "race-002", "race-003"),
        evidence_validator=lambda value: value.get("eligible") is True,
        race_validator=lambda value: value.get("ranking") == ["AAA", "BBB"],
        now=datetime(2026, 7, 26, 12, tzinfo=UTC),
    )

    assert resumed.valid_sources == (good_source,)
    assert resumed.pending_sources == (missing_source,)
    assert resumed.valid_evidence == ("AAA",)
    assert resumed.pending_evidence == ("BBB",)
    assert resumed.valid_races == ("race-001",)
    assert resumed.pending_races == ("race-002", "race-003")
    assert resumed.manifest.lifecycle[-1].event == "resumed"


def test_resume_rejects_schema_or_engine_mismatch(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    manifest = _create(store)
    raw = read_json(store.paths(manifest.id).manifest)
    raw["engine_version"] = "obsolete"
    atomic_write_json(store.paths(manifest.id).manifest, raw)

    with pytest.raises(IncompatibleRunError, match="uses engine"):
        store.resume(manifest.id)


def test_completion_requires_every_final_result_and_is_idempotent(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    manifest = _create(store)
    with pytest.raises(RunError, match="ranking.csv"):
        store.complete(manifest.id)

    atomic_write_csv(
        store.paths(manifest.id).ranking_csv,
        ("rank", "ticker"),
        ({"rank": 1, "ticker": "AAA"}, {"rank": 2, "ticker": "BBB"}),
    )
    with pytest.raises(RunError, match="ranking.json"):
        store.complete(manifest.id)
    atomic_write_json(
        store.paths(manifest.id).ranking_json,
        {
            "ranking": [
                {"rank": 1, "ticker": "AAA"},
                {"rank": 2, "ticker": "BBB"},
            ]
        },
    )
    atomic_write_text(store.paths(manifest.id).report, "# Ranking\n")
    completed = store.complete(
        manifest.id,
        now=datetime(2026, 7, 26, 18, 30, tzinfo=UTC),
    )
    repeated = store.complete(
        manifest.id,
        now=datetime(2026, 7, 27, 18, 30, tzinfo=UTC),
    )

    assert completed.status == "completed"
    assert completed.completed_at == "2026-07-26T18:30:00.000000Z"
    assert repeated == completed
    assert [event.event for event in repeated.lifecycle] == ["created", "completed"]
