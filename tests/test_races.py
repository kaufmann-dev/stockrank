from __future__ import annotations

from collections import Counter

import pytest

from stockrank.races import (
    RaceGroup,
    ScheduleError,
    is_connected,
    replacement_group,
    schedule,
)


@pytest.mark.parametrize(
    ("profile_name", "group_size", "minimum"),
    [
        ("low", 6, 4),
        ("medium", 5, 8),
        ("high", 4, 12),
    ],
)
def test_schedule_is_balanced_connected_and_deterministic(
    profile_name: str,
    group_size: int,
    minimum: int,
) -> None:
    tickers = [f"T{index:02d}" for index in range(50)]

    first = schedule(profile_name, tickers, seed=731)
    second = schedule(profile_name, tickers, seed=731)

    assert first == second
    assert all(len(group.tickers) == group_size for group in first.groups)
    assert all(len(set(group.tickers)) == group_size for group in first.groups)
    appearances = Counter(ticker for group in first.groups for ticker in group.tickers)
    assert min(appearances.values()) == minimum
    assert max(appearances.values()) - min(appearances.values()) <= 1
    assert set(appearances) == set(tickers)
    assert is_connected(tickers, (group.tickers for group in first.groups))


def test_different_seeds_change_schedule() -> None:
    tickers = [f"T{index:02d}" for index in range(20)]

    assert schedule("medium", tickers, 1).groups != schedule("medium", tickers, 2).groups


@pytest.mark.parametrize(
    ("profile", "tickers", "message"),
    [
        ("unknown", ["A", "B", "C", "D", "E", "F"], "unknown schedule"),
        ("low", ["A", "B", "C"], "at least 6"),
        ("high", ["A", "B", "C", "C"], "distinct"),
    ],
)
def test_schedule_rejects_invalid_inputs(
    profile: str,
    tickers: list[str],
    message: str,
) -> None:
    with pytest.raises(ScheduleError, match=message):
        schedule(profile, tickers, 1)


def test_replacement_is_deterministic_and_preserves_missing_coverage() -> None:
    failed = RaceGroup(index=7, tickers=("A", "B", "C", "D", "E"))

    first = replacement_group(failed, seed=44, attempt=1, index=20)
    second = replacement_group(failed, seed=44, attempt=1, index=20)

    assert first == second
    assert first.index == 20
    assert first.replacement_for == 7
    assert first.replacement_attempt == 1
    assert set(first.tickers) == set(failed.tickers)
    assert len(first.tickers) == len(set(first.tickers))


def test_connectivity_detects_disconnected_components() -> None:
    assert not is_connected(
        ("A", "B", "C", "D"),
        [("A", "B"), ("C", "D")],
    )
