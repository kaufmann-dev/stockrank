from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w


VALID_PHASES = {"scores", "proposals", "portfolio"}
PLACEHOLDER_TOKENS = {"...", "TBD", "TODO", "FAKE"}


class ConfigError(RuntimeError):
    pass


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing required file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a TOML table")
    return data


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)


def require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ConfigError(f"{label} must be a numeric weight")
    return float(value)


def require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True)
class DataConfig:
    price_history_years: int
    news_items: int
    financial_periods: int


@dataclass(frozen=True)
class GlobalConfig:
    api_provider: str
    data: DataConfig
    default_strategy: str
    benchmark: str


@dataclass(frozen=True)
class Universe:
    name: str
    tickers: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Mode:
    name: str
    phase: str | None
    persona: str
    task: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Harness:
    name: str
    description: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class HarnessWeight:
    name: str
    weight: float


@dataclass(frozen=True)
class ScoreMode:
    name: str
    weight: float
    harnesses: list[HarnessWeight]


@dataclass(frozen=True)
class ProposalPair:
    mode: str
    harness: str


@dataclass(frozen=True)
class ProposalConfig:
    num_positions: int
    runs: list[ProposalPair]


@dataclass(frozen=True)
class PortfolioConfig:
    mode: str
    harness: str


@dataclass(frozen=True)
class Strategy:
    name: str
    universe: str
    score_modes: list[ScoreMode]
    proposals: ProposalConfig
    portfolio: PortfolioConfig
    raw: dict[str, Any]


@dataclass(frozen=True)
class ResolvedStrategy:
    strategy: Strategy
    universe: Universe
    modes: dict[str, Mode]
    harnesses: dict[str, Harness]
    planned_score_evaluations: int


def load_global_config(root: Path) -> GlobalConfig:
    raw = read_toml(root / "stockrank.toml")
    api = raw.get("api", {})
    data = raw.get("data", {})
    defaults = raw.get("defaults", {})
    tracking = raw.get("tracking", {})
    provider = require_str(api.get("provider"), "api.provider")
    if provider != "massive":
        raise ConfigError("api.provider must be 'massive'")
    data_config = DataConfig(
        price_history_years=int(require_number(data.get("price_history_years"), "data.price_history_years")),
        news_items=int(require_number(data.get("news_items"), "data.news_items")),
        financial_periods=int(require_number(data.get("financial_periods"), "data.financial_periods")),
    )
    return GlobalConfig(
        api_provider=provider,
        data=data_config,
        default_strategy=require_str(defaults.get("strategy"), "defaults.strategy"),
        benchmark=require_str(tracking.get("benchmark"), "tracking.benchmark").upper(),
    )


def load_universe(root: Path, name: str) -> Universe:
    raw = read_toml(root / "universes" / f"{name}.toml")
    tickers = raw.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise ConfigError(f"universe {name} must define non-empty tickers")
    normalized: list[str] = []
    for index, ticker in enumerate(tickers):
        text = require_str(ticker, f"universe {name} tickers[{index}]").upper()
        if text in PLACEHOLDER_TOKENS or "..." in text or "FAKE" in text:
            raise ConfigError(f"universe {name} contains placeholder ticker {text!r}")
        normalized.append(text)
    if len(normalized) != len(set(normalized)):
        raise ConfigError(f"universe {name} contains duplicate tickers")
    return Universe(name=require_str(raw.get("name"), f"universe {name}.name"), tickers=normalized, raw=raw)


def load_mode(root: Path, name: str) -> Mode:
    raw = read_toml(root / "modes" / f"{name}.toml")
    phase = raw.get("phase")
    if phase is not None:
        phase = require_str(phase, f"mode {name}.phase")
        if phase not in VALID_PHASES:
            raise ConfigError(f"mode {name}.phase must be one of {sorted(VALID_PHASES)}")
    return Mode(
        name=require_str(raw.get("name"), f"mode {name}.name"),
        phase=phase,
        persona=require_str(raw.get("persona"), f"mode {name}.persona"),
        task=require_str(raw.get("task"), f"mode {name}.task"),
        raw=raw,
    )


def load_harness(root: Path, name: str) -> Harness:
    raw = read_toml(root / "harnesses" / f"{name}.toml")
    return Harness(
        name=require_str(raw.get("name"), f"harness {name}.name"),
        description=require_str(raw.get("description"), f"harness {name}.description"),
        raw=raw,
    )


def load_strategy(root: Path, name: str) -> Strategy:
    raw = read_toml(root / "strategies" / f"{name}.toml")
    scores = raw.get("scores", {})
    score_modes_raw = scores.get("modes")
    if not isinstance(score_modes_raw, list) or not score_modes_raw:
        raise ConfigError(f"strategy {name} must define [[scores.modes]]")
    score_modes: list[ScoreMode] = []
    for mode_index, mode_raw in enumerate(score_modes_raw):
        if not isinstance(mode_raw, dict):
            raise ConfigError(f"strategy {name} scores.modes[{mode_index}] must be a table")
        harnesses_raw = mode_raw.get("harnesses")
        if not isinstance(harnesses_raw, list) or not harnesses_raw:
            raise ConfigError(f"strategy {name} score mode {mode_index} must define harnesses")
        harnesses: list[HarnessWeight] = []
        for harness_index, harness_raw in enumerate(harnesses_raw):
            if not isinstance(harness_raw, dict):
                raise ConfigError(f"strategy {name} harness ref {mode_index}.{harness_index} must be a table")
            harnesses.append(
                HarnessWeight(
                    name=require_str(harness_raw.get("name"), f"strategy {name} harness.name"),
                    weight=require_number(harness_raw.get("weight"), f"strategy {name} harness.weight"),
                )
            )
        score_modes.append(
            ScoreMode(
                name=require_str(mode_raw.get("name"), f"strategy {name} scores.modes.name"),
                weight=require_number(mode_raw.get("weight"), f"strategy {name} scores.modes.weight"),
                harnesses=harnesses,
            )
        )
    proposals_raw = raw.get("proposals")
    if not isinstance(proposals_raw, dict):
        raise ConfigError(f"strategy {name} must define [proposals]")
    runs_raw = proposals_raw.get("runs")
    if not isinstance(runs_raw, list) or not runs_raw:
        raise ConfigError(f"strategy {name} proposals.runs must be non-empty")
    proposal_runs: list[ProposalPair] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, pair_raw in enumerate(runs_raw):
        if not isinstance(pair_raw, dict):
            raise ConfigError(f"strategy {name} proposals.runs[{index}] must be a table")
        pair = ProposalPair(
            mode=require_str(pair_raw.get("mode"), f"strategy {name} proposals.runs.mode"),
            harness=require_str(pair_raw.get("harness"), f"strategy {name} proposals.runs.harness"),
        )
        key = (pair.mode, pair.harness)
        if key in seen_pairs:
            raise ConfigError(f"strategy {name} has duplicate proposal pair {pair.mode}__{pair.harness}")
        seen_pairs.add(key)
        proposal_runs.append(pair)
    portfolio_raw = raw.get("portfolio")
    if not isinstance(portfolio_raw, dict):
        raise ConfigError(f"strategy {name} must define [portfolio]")
    return Strategy(
        name=require_str(raw.get("name"), f"strategy {name}.name"),
        universe=require_str(raw.get("universe"), f"strategy {name}.universe"),
        score_modes=score_modes,
        proposals=ProposalConfig(
            num_positions=int(require_number(proposals_raw.get("num_positions"), f"strategy {name} proposals.num_positions")),
            runs=proposal_runs,
        ),
        portfolio=PortfolioConfig(
            mode=require_str(portfolio_raw.get("mode"), f"strategy {name} portfolio.mode"),
            harness=require_str(portfolio_raw.get("harness"), f"strategy {name} portfolio.harness"),
        ),
        raw=raw,
    )


def resolve_strategy(root: Path, name: str) -> ResolvedStrategy:
    strategy = load_strategy(root, name)
    universe = load_universe(root, strategy.universe)
    modes: dict[str, Mode] = {}
    harnesses: dict[str, Harness] = {}

    def add_mode(mode_name: str, phase: str) -> None:
        mode = modes.get(mode_name) or load_mode(root, mode_name)
        if mode.name != mode_name:
            raise ConfigError(f"mode file {mode_name}.toml declares name {mode.name!r}")
        if mode.phase is not None and mode.phase != phase:
            raise ConfigError(f"mode {mode_name} declares phase {mode.phase!r} but strategy uses it for {phase!r}")
        modes[mode_name] = mode

    def add_harness(harness_name: str) -> None:
        harness = harnesses.get(harness_name) or load_harness(root, harness_name)
        if harness.name != harness_name:
            raise ConfigError(f"harness file {harness_name}.toml declares name {harness.name!r}")
        harnesses[harness_name] = harness

    planned = 0
    for score_mode in strategy.score_modes:
        add_mode(score_mode.name, "scores")
        planned += len(score_mode.harnesses) * len(universe.tickers)
        for harness in score_mode.harnesses:
            add_harness(harness.name)
    for pair in strategy.proposals.runs:
        add_mode(pair.mode, "proposals")
        add_harness(pair.harness)
    add_mode(strategy.portfolio.mode, "portfolio")
    add_harness(strategy.portfolio.harness)
    return ResolvedStrategy(
        strategy=strategy,
        universe=universe,
        modes=modes,
        harnesses=harnesses,
        planned_score_evaluations=planned,
    )


def available_names(root: Path, folder: str) -> list[str]:
    path = root / folder
    if not path.exists():
        return []
    return sorted(p.stem for p in path.glob("*.toml") if p.is_file())


def build_run_manifest(resolved: ResolvedStrategy, run_id: int, created_iso: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "created": created_iso,
        "universe_name": resolved.universe.name,
        "tickers": resolved.universe.tickers,
        "strategy": resolved.strategy.raw,
        "snapshots": {
            "modes": [resolved.modes[name].raw for name in sorted(resolved.modes)],
            "harnesses": [resolved.harnesses[name].raw for name in sorted(resolved.harnesses)],
        },
    }


def load_run_manifest(root: Path, run_id: str | int) -> dict[str, Any]:
    return read_toml(root / "runs" / str(run_id) / "run.toml")


def update_run_manifest(root: Path, run_id: str | int, manifest: dict[str, Any]) -> None:
    write_toml(root / "runs" / str(run_id) / "run.toml", manifest)


def run_ids(root: Path) -> list[str]:
    runs = root / "runs"
    if not runs.exists():
        return []
    return sorted((p.name for p in runs.iterdir() if p.is_dir()), key=lambda value: int(value) if value.isdigit() else -1)


def is_finalized(root: Path, run_id: str | int, phase: str) -> bool:
    run = root / "runs" / str(run_id)
    if phase == "scores":
        return all((run / "scores" / name).exists() for name in ("scores.csv", "rationales.csv", "report.md"))
    if phase == "proposals":
        return all((run / "proposals" / name).exists() for name in ("proposals.csv", "proposals.md"))
    if phase == "portfolio":
        return all((run / "portfolio" / name).exists() for name in ("portfolio.csv", "trades.csv", "report.md"))
    raise ConfigError(f"unknown phase {phase!r}")


def latest_run(root: Path, finalized_phase: str | None = None) -> str:
    candidates = run_ids(root)
    if finalized_phase:
        candidates = [run_id for run_id in candidates if is_finalized(root, run_id, finalized_phase)]
    if not candidates:
        detail = f" with finalized {finalized_phase}" if finalized_phase else ""
        raise ConfigError(f"no runs found{detail}")
    return candidates[-1]


def mode_snapshot(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for raw in manifest.get("snapshots", {}).get("modes", []):
        if raw.get("name") == name:
            return raw
    raise ConfigError(f"run manifest is missing mode snapshot {name!r}")


def harness_snapshot(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for raw in manifest.get("snapshots", {}).get("harnesses", []):
        if raw.get("name") == name:
            return raw
    raise ConfigError(f"run manifest is missing harness snapshot {name!r}")


def score_modes_from_manifest(manifest: dict[str, Any]) -> list[ScoreMode]:
    raw_modes = manifest.get("strategy", {}).get("scores", {}).get("modes", [])
    parsed: list[ScoreMode] = []
    for mode_raw in raw_modes:
        parsed.append(
            ScoreMode(
                name=str(mode_raw["name"]),
                weight=float(mode_raw["weight"]),
                harnesses=[HarnessWeight(name=str(h["name"]), weight=float(h["weight"])) for h in mode_raw["harnesses"]],
            )
        )
    return parsed


def proposal_pairs_from_manifest(manifest: dict[str, Any]) -> list[ProposalPair]:
    return [
        ProposalPair(mode=str(raw["mode"]), harness=str(raw["harness"]))
        for raw in manifest.get("strategy", {}).get("proposals", {}).get("runs", [])
    ]


def portfolio_pair_from_manifest(manifest: dict[str, Any]) -> PortfolioConfig:
    raw = manifest.get("strategy", {}).get("portfolio", {})
    return PortfolioConfig(mode=str(raw["mode"]), harness=str(raw["harness"]))


def score_mode_summary(manifest: dict[str, Any]) -> str:
    return ", ".join(f"{mode.name}x{mode.weight:g}" for mode in score_modes_from_manifest(manifest))
