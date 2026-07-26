from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


class ScheduleError(ValueError):
    """Raised when a valid race schedule cannot be constructed."""


@dataclass(frozen=True)
class ScheduleProfile:
    name: str
    group_size: int
    min_appearances: int


SCHEDULE_PROFILES: dict[str, ScheduleProfile] = {
    "low": ScheduleProfile(name="low", group_size=6, min_appearances=4),
    "medium": ScheduleProfile(name="medium", group_size=5, min_appearances=8),
    "high": ScheduleProfile(name="high", group_size=4, min_appearances=12),
}


@dataclass(frozen=True)
class RaceGroup:
    index: int
    tickers: tuple[str, ...]
    replacement_for: int | None = None
    replacement_attempt: int = 0


@dataclass(frozen=True)
class RaceSchedule:
    profile: ScheduleProfile
    seed: int
    groups: tuple[RaceGroup, ...]
    generation_attempt: int = 0

    @property
    def appearances(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for group in self.groups:
            counts.update(group.tickers)
        return dict(counts)


def schedule(
    profile_name: str,
    tickers: Sequence[str],
    seed: int,
) -> RaceSchedule:
    """Build a deterministic, balanced, connected race schedule."""

    try:
        profile = SCHEDULE_PROFILES[profile_name]
    except KeyError as exc:
        raise ScheduleError(
            f"unknown schedule profile {profile_name!r}; choose one of {sorted(SCHEDULE_PROFILES)}"
        ) from exc
    universe = _validate_tickers(tickers)
    if len(universe) < profile.group_size:
        raise ScheduleError(f"profile {profile.name} requires at least {profile.group_size} tickers")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ScheduleError("schedule seed must be an integer")

    for generation_attempt in range(32):
        groups = _build_groups(profile, universe, seed, generation_attempt)
        if is_connected(universe, (group.tickers for group in groups)):
            return RaceSchedule(
                profile=profile,
                seed=seed,
                groups=tuple(groups),
                generation_attempt=generation_attempt,
            )
    raise ScheduleError("could not construct a connected schedule after 32 attempts")


def replacement_group(
    failed_group: RaceGroup,
    seed: int,
    attempt: int,
    *,
    index: int | None = None,
) -> RaceGroup:
    """Create a deterministic retry preserving the failed group's coverage."""

    if attempt < 1:
        raise ScheduleError("replacement attempt must be at least 1")
    if len(failed_group.tickers) != len(set(failed_group.tickers)):
        raise ScheduleError("failed group contains duplicate tickers")
    ordered = list(failed_group.tickers)
    random.Random(f"{seed}:{failed_group.index}:{attempt}:replacement").shuffle(ordered)
    return RaceGroup(
        index=failed_group.index if index is None else index,
        tickers=tuple(ordered),
        replacement_for=(
            failed_group.index if failed_group.replacement_for is None else failed_group.replacement_for
        ),
        replacement_attempt=attempt,
    )


def is_connected(
    tickers: Sequence[str],
    groups: Iterable[Sequence[str]],
) -> bool:
    universe = tuple(tickers)
    if not universe:
        return False
    adjacency = {ticker: set() for ticker in universe}
    for group in groups:
        members = set(group)
        for ticker in members:
            if ticker in adjacency:
                adjacency[ticker].update(members - {ticker})
    seen: set[str] = set()
    pending = [universe[0]]
    while pending:
        ticker = pending.pop()
        if ticker in seen:
            continue
        seen.add(ticker)
        pending.extend(adjacency[ticker] - seen)
    return seen == set(universe)


def _build_groups(
    profile: ScheduleProfile,
    tickers: tuple[str, ...],
    seed: int,
    generation_attempt: int,
) -> list[RaceGroup]:
    count = len(tickers)
    group_size = profile.group_size
    group_count = math.ceil(count * profile.min_appearances / group_size)
    extra_slots = group_count * group_size - count * profile.min_appearances
    rng = random.Random(f"{seed}:{generation_attempt}:schedule")

    extra_order = list(range(count))
    rng.shuffle(extra_order)
    remaining = [profile.min_appearances] * count
    for ticker_index in extra_order[:extra_slots]:
        remaining[ticker_index] += 1

    pair_counts = [[0] * count for _ in range(count)]
    groups: list[RaceGroup] = []
    for group_index in range(group_count):
        groups_left = group_count - group_index
        tie_order = list(range(count))
        rng.shuffle(tie_order)
        tie_rank = {ticker_index: rank for rank, ticker_index in enumerate(tie_order)}

        selected = [ticker_index for ticker_index in range(count) if remaining[ticker_index] == groups_left]
        if len(selected) > group_size:
            raise ScheduleError("balanced schedule construction became infeasible")
        selected.sort(key=tie_rank.__getitem__)

        while len(selected) < group_size:
            candidates = [
                ticker_index
                for ticker_index in range(count)
                if remaining[ticker_index] > 0 and ticker_index not in selected
            ]
            if not candidates:
                raise ScheduleError("balanced schedule has too few distinct candidates")

            selected.append(
                min(
                    candidates,
                    key=lambda ticker_index: _candidate_score(
                        ticker_index,
                        selected,
                        remaining,
                        pair_counts,
                        tie_rank,
                    ),
                )
            )

        for ticker_index in selected:
            remaining[ticker_index] -= 1
        for offset, ticker_index in enumerate(selected):
            for other in selected[offset + 1 :]:
                pair_counts[ticker_index][other] += 1
                pair_counts[other][ticker_index] += 1

        groups.append(
            RaceGroup(
                index=group_index,
                tickers=tuple(tickers[ticker_index] for ticker_index in selected),
            )
        )

    if any(remaining):
        raise ScheduleError("balanced schedule did not consume every appearance")
    return groups


def _candidate_score(
    ticker_index: int,
    selected: Sequence[int],
    remaining: Sequence[int],
    pair_counts: Sequence[Sequence[int]],
    tie_rank: dict[int, int],
) -> tuple[int, int, int]:
    repeated_pairs = sum(pair_counts[ticker_index][other] for other in selected)
    return (
        -remaining[ticker_index],
        repeated_pairs,
        tie_rank[ticker_index],
    )


def _validate_tickers(tickers: Sequence[str]) -> tuple[str, ...]:
    universe = tuple(tickers)
    if any(not isinstance(ticker, str) or not ticker.strip() for ticker in universe):
        raise ScheduleError("schedule tickers must be non-empty strings")
    if len(universe) != len(set(universe)):
        raise ScheduleError("schedule tickers must be distinct")
    return universe
