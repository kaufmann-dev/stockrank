from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from openai import OpenAI
from rich.console import Console

from .config import (
    AppConfig,
    ConfigError,
    DataConfig,
    MassiveConfig,
    Mode,
    ModelConfig,
    SecConfig,
    load_app_config,
    load_environment,
    load_mode,
    profile_name,
    require_environment,
)
from .evidence import (
    EvidencePack,
    EvidenceSettings,
    TickerSourceBundle,
    build_evidence_packs_from_sources,
    fetch_source_bundles,
)
from .llm import OpenAIRaceJudge, RaceModelError, RaceResult, validate_race_result
from .massive import MassiveClient
from .races import RaceGroup, RaceSchedule, replacement_group, schedule
from .ranking import DEFAULT_BOOTSTRAP_SAMPLES, fit_ranking
from .reporting import ranking_csv, ranking_payload, ranking_report
from .runs import (
    RunError,
    RunManifest,
    RunPaths,
    RunStore,
    atomic_write_json,
    atomic_write_text,
    read_json,
)
from .sec import SecClient
from .tracking import TrackingReport, track_all, track_run
from .universe import FrozenUniverse, load_universe_spec
from .universe import resolve_universe as resolve_universe_spec

MAX_RACE_ATTEMPTS = 3
SOURCE_PROVIDER = "source-bundle"


class RaceJudge(Protocol):
    def judge(
        self,
        tickers: Sequence[str],
        evidence_by_ticker: Mapping[str, object],
        mode_prompt: str,
        allowed_evidence_ids: Mapping[str, Collection[str]] | None = None,
    ) -> RaceResult: ...


def execute_rank(
    root: Path,
    *,
    universe_name: str | None = None,
    mode_name: str | None = None,
    model_name: str | None = None,
    profile_name: str | None = None,
    resume_id: str | None = None,
    console: Console | None = None,
    now: datetime | None = None,
    massive_client: MassiveClient | None = None,
    sec_client: SecClient | None = None,
    judge: RaceJudge | None = None,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
) -> str:
    """Run or resume the complete ranking pipeline."""

    root = Path(root)
    console = console or Console()
    current = _utc_now(now)
    load_environment(root)
    store = RunStore(root)
    manifest: RunManifest | None = None
    try:
        if resume_id is None:
            if universe_name is None:
                raise ConfigError("universe_name is required for a new run")
            preflight_config = load_app_config(root)
            selected_model_name = model_name or preflight_config.defaults.model
            try:
                preflight_model = preflight_config.models[selected_model_name]
            except KeyError as exc:
                raise ConfigError(f"unknown model profile {selected_model_name!r}") from exc
            _require_credentials(
                preflight_config.massive,
                preflight_config.sec,
                preflight_model,
                massive_supplied=massive_client is not None,
                sec_supplied=sec_client is not None,
                judge_supplied=judge is not None,
            )
            (
                manifest,
                source_config,
                sec_config,
                data_config,
                model,
                mode,
                frozen_universe,
            ) = _create_run(
                root,
                store,
                universe_name=universe_name,
                mode_name=mode_name,
                model_name=model_name,
                requested_profile=profile_name,
                now=current,
                massive_client=massive_client,
            )
        else:
            if any(value is not None for value in (universe_name, mode_name, model_name, profile_name)):
                raise ConfigError("resume cannot change universe, mode, model, or profile")
            manifest = store.load(resume_id)
            source_config, sec_config, data_config = _settings_from_manifest(manifest)
            model = _model_from_snapshot(manifest.model)
            mode = _mode_from_snapshot(manifest.mode)
            frozen_universe = _universe_from_snapshot(manifest.universe)
            _require_credentials(
                source_config,
                sec_config,
                model,
                massive_supplied=massive_client is not None,
                sec_supplied=sec_client is not None,
                judge_supplied=judge is not None,
            )
            store.resume(resume_id, now=current)

        massive = massive_client or _massive_client(source_config)
        sec = sec_client or _sec_client(sec_config)
        race_judge = judge or OpenAIRaceJudge(model)
        paths = store.paths(manifest.id)
        evidence_settings = _evidence_settings(data_config)

        bundles = _load_or_fetch_sources(
            paths,
            frozen_universe,
            massive,
            sec,
            evidence_settings,
            concurrency=source_config.concurrency,
            console=console,
        )
        packs = _load_or_build_evidence(
            paths,
            bundles,
            frozen_universe,
            evidence_settings,
            console,
        )
        eligible = tuple(pack.ticker for pack in packs if pack.eligible)
        exclusions = tuple(
            {
                "ticker": pack.ticker,
                "reason": pack.exclusion_reason or "not eligible",
            }
            for pack in packs
            if not pack.eligible
        )
        race_schedule = _load_or_create_schedule(
            paths,
            manifest.profile,
            eligible,
            manifest.seed,
        )
        completed = _run_races(
            paths,
            race_schedule,
            {pack.ticker: pack for pack in packs if pack.eligible},
            mode.prompt,
            race_judge,
            model.concurrency,
            console,
        )
        result = fit_ranking(
            eligible,
            completed,
            manifest.seed,
            min_appearances=race_schedule.profile.min_appearances,
            bootstrap_samples=bootstrap_samples,
        )
        completed_at = _utc_now(now)
        run_metadata = {
            "run_id": manifest.id,
            "universe_name": str(manifest.universe.get("name", "")),
            "mode_name": mode.name,
            "model_name": model.name,
            "profile": manifest.profile,
            "created_at": manifest.created_at,
            "completed_at": _timestamp(completed_at),
        }
        atomic_write_text(paths.ranking_csv, ranking_csv(result))
        atomic_write_json(
            paths.ranking_json,
            ranking_payload(result, run=run_metadata, exclusions=exclusions),
        )
        atomic_write_text(
            paths.report,
            ranking_report(result, run=run_metadata, exclusions=exclusions),
        )
        store.complete(manifest.id, now=completed_at)
        return manifest.id
    except Exception as exc:
        if manifest is not None:
            try:
                latest = store.load(manifest.id)
                if latest.status != "completed":
                    store.fail(manifest.id, _safe_failure(exc), now=_utc_now(now))
            except (OSError, RunError, ValueError) as failure_error:
                console.print(
                    f"[yellow]warning:[/] could not record the run failure: {_safe_failure(failure_error)}"
                )
        raise


def resolve_universe(
    root: Path,
    name: str,
    *,
    console: Console | None = None,
    as_of: date | None = None,
    massive_client: MassiveClient | None = None,
) -> dict[str, object]:
    """Resolve one saved universe definition without creating a run."""

    del console
    root = Path(root)
    load_environment(root)
    config = load_app_config(root)
    client = massive_client or _massive_client(config.massive)
    spec = load_universe_spec(root / "universes" / f"{name}.toml")
    resolved = resolve_universe_spec(spec, client, as_of or datetime.now(UTC).date())
    return resolved.as_dict()


def test_model(
    root: Path,
    name: str | None = None,
    *,
    client_factory: Any = OpenAI,
) -> str:
    """Check authentication and configured model availability with no completion call."""

    root = Path(root)
    load_environment(root)
    config = load_app_config(root)
    selected = name or config.defaults.model
    try:
        model = config.models[selected]
    except KeyError as exc:
        raise ConfigError(f"unknown model profile {selected!r}") from exc
    client = client_factory(
        api_key=require_environment(model.api_key_env),
        base_url=model.base_url,
        timeout=model.timeout_seconds,
        max_retries=model.max_retries,
    )
    response = client.models.list()
    available = {
        item.id for item in getattr(response, "data", ()) if isinstance(getattr(item, "id", None), str)
    }
    if model.model not in available:
        shown = ", ".join(sorted(available)) if available else "(none returned)"
        raise ConfigError(
            f"model profile {selected!r} selects {model.model!r}, but the provider reported: {shown}"
        )
    return selected


def track_rankings(
    root: Path,
    *,
    run_id: str | None,
    all_runs: bool,
    console: Console | None = None,
    massive_client: MassiveClient | None = None,
    end_date: date | None = None,
) -> list[str]:
    """Update forward results for one or every completed ranking."""

    root = Path(root)
    console = console or Console()
    load_environment(root)
    config = load_app_config(root)
    client = massive_client or _massive_client(config.massive)
    if all_runs:
        reports = track_all(
            root,
            cast(Any, client),
            config.tracking.benchmark,
            end_date=end_date,
        )
    elif run_id is not None:
        reports = [
            track_run(
                root,
                run_id,
                cast(Any, client),
                config.tracking.benchmark,
                end_date=end_date,
            )
        ]
    else:
        raise ConfigError("track requires run_id or all_runs=True")
    for report in reports:
        _print_tracking(console, report)
    return [report.run_id for report in reports]


def _create_run(
    root: Path,
    store: RunStore,
    *,
    universe_name: str,
    mode_name: str | None,
    model_name: str | None,
    requested_profile: str | None,
    now: datetime,
    massive_client: MassiveClient | None,
) -> tuple[
    RunManifest,
    MassiveConfig,
    SecConfig,
    DataConfig,
    ModelConfig,
    Mode,
    FrozenUniverse,
]:
    config = load_app_config(root)
    selected_mode = mode_name or config.defaults.mode
    selected_model = model_name or config.defaults.model
    selected_profile = profile_name(root, requested_profile)
    try:
        model = config.models[selected_model]
    except KeyError as exc:
        raise ConfigError(f"unknown model profile {selected_model!r}") from exc
    mode = load_mode(root, selected_mode)
    massive = massive_client or _massive_client(config.massive)
    spec = load_universe_spec(root / "universes" / f"{universe_name}.toml")
    frozen = resolve_universe_spec(spec, massive, now.date())
    manifest = store.create(
        universe=frozen.as_dict(),
        mode={
            "name": mode.name,
            "rank_1_meaning": mode.rank_1_meaning,
            "prompt": mode.prompt,
        },
        model=asdict(model),
        settings=_settings_snapshot(config),
        profile=selected_profile,
        seed=config.defaults.seed,
        now=now,
    )
    return (
        manifest,
        config.massive,
        config.sec,
        config.data,
        model,
        mode,
        frozen,
    )


def _settings_snapshot(config: AppConfig) -> dict[str, Any]:
    return {
        "data": asdict(config.data),
        "sources": {
            "massive": asdict(config.massive),
            "sec": asdict(config.sec),
        },
    }


def _settings_from_manifest(
    manifest: RunManifest,
) -> tuple[MassiveConfig, SecConfig, DataConfig]:
    settings = manifest.settings
    sources = _mapping(settings.get("sources"), "manifest settings.sources")
    massive = _mapping(sources.get("massive"), "manifest settings.sources.massive")
    sec = _mapping(sources.get("sec"), "manifest settings.sources.sec")
    data = _mapping(settings.get("data"), "manifest settings.data")
    try:
        return (
            MassiveConfig(
                api_key_env=_required_string(massive, "api_key_env"),
                base_url=_required_string(massive, "base_url").rstrip("/"),
                concurrency=_required_int(massive, "concurrency", 1),
            ),
            SecConfig(
                user_agent_env=_required_string(sec, "user_agent_env"),
                base_url=_required_string(sec, "base_url").rstrip("/"),
                requests_per_second=_required_float(sec, "requests_per_second", 0.1),
            ),
            DataConfig(
                price_history_days=_required_int(data, "price_history_days", 60),
                liquidity_lookback_sessions=_required_int(data, "liquidity_lookback_sessions", 1),
                news_items=_required_int(data, "news_items", 0),
                filing_items=_required_int(data, "filing_items", 0),
                insider_items=_required_int(data, "insider_items", 0),
                min_price_bars=_required_int(data, "min_price_bars", 2),
                evidence_char_budget=_required_int(data, "evidence_char_budget", 1000),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise RunError(f"run {manifest.id} has invalid frozen settings: {exc}") from exc


def _model_from_snapshot(raw: Mapping[str, Any]) -> ModelConfig:
    extra_body = _mapping(raw.get("extra_body", {}), "manifest model.extra_body")
    reasoning = raw.get("reasoning_effort")
    if reasoning is not None and (not isinstance(reasoning, str) or not reasoning.strip()):
        raise RunError("manifest model.reasoning_effort must be a non-empty string or null")
    return ModelConfig(
        name=_required_string(raw, "name"),
        base_url=_required_string(raw, "base_url").rstrip("/"),
        model=_required_string(raw, "model"),
        api_key_env=_required_string(raw, "api_key_env"),
        timeout_seconds=_required_float(raw, "timeout_seconds", 0.1),
        max_retries=_required_int(raw, "max_retries", 0),
        concurrency=_required_int(raw, "concurrency", 1),
        max_tokens=_required_int(raw, "max_tokens", 1),
        reasoning_effort=reasoning.strip() if isinstance(reasoning, str) else None,
        extra_body=dict(extra_body),
    )


def _mode_from_snapshot(raw: Mapping[str, Any]) -> Mode:
    return Mode(
        name=_required_string(raw, "name"),
        rank_1_meaning=_required_string(raw, "rank_1_meaning"),
        prompt=_required_string(raw, "prompt"),
        raw=dict(raw),
    )


def _universe_from_snapshot(raw: Mapping[str, Any]) -> FrozenUniverse:
    name = _required_string(raw, "name")
    kind = _required_string(raw, "kind")
    as_of_raw = _required_string(raw, "as_of")
    tickers_raw = raw.get("tickers")
    if kind not in {"static", "liquidity"}:
        raise RunError("manifest universe.kind must be static or liquidity")
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise RunError("manifest universe.tickers must be a non-empty array")
    tickers = tuple(str(ticker).upper() for ticker in tickers_raw)
    if any(not ticker for ticker in tickers) or len(tickers) != len(set(tickers)):
        raise RunError("manifest universe.tickers must contain distinct non-empty strings")
    try:
        as_of = date.fromisoformat(as_of_raw)
    except ValueError as exc:
        raise RunError("manifest universe.as_of must be an ISO date") from exc
    return FrozenUniverse(
        name=name,
        kind=cast(Any, kind),
        as_of=as_of,
        tickers=tickers,
    )


def _evidence_settings(data: DataConfig) -> EvidenceSettings:
    return EvidenceSettings(
        price_history_days=data.price_history_days,
        min_price_bars=data.min_price_bars,
        news_items=data.news_items,
        filing_items=data.filing_items,
        insider_items=data.insider_items,
        evidence_char_budget=data.evidence_char_budget,
    )


def _load_or_fetch_sources(
    paths: RunPaths,
    universe: FrozenUniverse,
    massive: MassiveClient,
    sec: SecClient,
    settings: EvidenceSettings,
    *,
    concurrency: int,
    console: Console,
) -> tuple[TickerSourceBundle, ...]:
    bundles: dict[str, TickerSourceBundle] = {}
    pending: list[str] = []
    for ticker in universe.tickers:
        path = paths.raw_source(SOURCE_PROVIDER, ticker, "bundle")
        try:
            bundle = TickerSourceBundle.from_dict(_object(read_json(path), str(path)))
            expected_start = universe.as_of - timedelta(days=settings.price_history_days)
            if (
                bundle.ticker != ticker
                or bundle.as_of != universe.as_of
                or bundle.price_start != expected_start
            ):
                raise ValueError("source bundle identity does not match the frozen universe")
            bundles[ticker] = bundle
        except (OSError, RunError, TypeError, ValueError, KeyError):
            pending.append(ticker)

    if pending:
        console.print(f"Fetching source bundles for {len(pending)} ticker(s)…")
        workers = min(concurrency, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures: dict[Future[TickerSourceBundle], str] = {
                executor.submit(
                    _fetch_one_bundle,
                    ticker,
                    massive,
                    sec,
                    universe.as_of,
                    settings,
                ): ticker
                for ticker in pending
            }
            for future in as_completed(futures):
                ticker = futures[future]
                bundle = future.result()
                if bundle.ticker != ticker:
                    raise RunError(f"source fetch returned {bundle.ticker!r} for requested ticker {ticker!r}")
                atomic_write_json(
                    paths.raw_source(SOURCE_PROVIDER, ticker, "bundle"),
                    bundle.as_dict(),
                )
                bundles[ticker] = bundle
    return tuple(bundles[ticker] for ticker in universe.tickers)


def _fetch_one_bundle(
    ticker: str,
    massive: MassiveClient,
    sec: SecClient,
    as_of: date,
    settings: EvidenceSettings,
) -> TickerSourceBundle:
    return fetch_source_bundles(
        (ticker,),
        massive,
        sec,
        as_of=as_of,
        settings=settings,
    )[0]


def _load_or_build_evidence(
    paths: RunPaths,
    bundles: Sequence[TickerSourceBundle],
    universe: FrozenUniverse,
    settings: EvidenceSettings,
    console: Console,
) -> tuple[EvidencePack, ...]:
    derived = build_evidence_packs_from_sources(
        tuple(bundles),
        as_of=universe.as_of,
        settings=settings,
    )
    selected: list[EvidencePack] = []
    built = 0
    for pack in derived:
        path = paths.evidence(pack.ticker)
        try:
            existing = EvidencePack.from_dict(_object(read_json(path), str(path)))
        except (OSError, RunError, TypeError, ValueError, KeyError):
            atomic_write_json(path, pack.as_dict())
            selected.append(pack)
            built += 1
            continue
        if existing.ticker != pack.ticker or existing.as_of != universe.as_of:
            raise RunError(f"stored evidence at {path} does not match the frozen universe identity")
        if existing.as_dict() != pack.as_dict():
            raise RunError(f"stored evidence for {pack.ticker} does not match its frozen source bundle")
        selected.append(existing)
    if built:
        console.print(f"Built {built} evidence pack(s).")
    return tuple(selected)


def _load_or_create_schedule(
    paths: RunPaths,
    profile: str,
    tickers: Sequence[str],
    seed: int,
) -> RaceSchedule:
    expected = schedule(profile, tickers, seed)
    expected_payload = _schedule_payload(expected)
    if paths.schedule.exists():
        stored = read_json(paths.schedule)
        if stored != expected_payload:
            raise RunError("stored schedule does not match the deterministic schedule for frozen inputs")
    else:
        atomic_write_json(paths.schedule, expected_payload)
    return expected


def _schedule_payload(value: RaceSchedule) -> dict[str, Any]:
    return {
        "profile": asdict(value.profile),
        "seed": value.seed,
        "generation_attempt": value.generation_attempt,
        "groups": [
            {
                "index": group.index,
                "tickers": list(group.tickers),
                "replacement_for": group.replacement_for,
                "replacement_attempt": group.replacement_attempt,
            }
            for group in value.groups
        ],
    }


def _run_races(
    paths: RunPaths,
    race_schedule: RaceSchedule,
    packs: Mapping[str, EvidencePack],
    mode_prompt: str,
    judge: RaceJudge,
    concurrency: int,
    console: Console,
) -> tuple[RaceResult, ...]:
    completed: dict[int, RaceResult] = {}
    pending: dict[int, int] = {}
    for base in race_schedule.groups:
        for attempt in range(MAX_RACE_ATTEMPTS):
            candidate = _candidate_group(base, race_schedule.seed, attempt)
            race_id = _race_id(base.index, attempt)
            response = _read_valid_race(
                paths,
                race_id,
                candidate,
                packs,
                mode_prompt,
            )
            if response is not None:
                completed[base.index] = response
                break
        else:
            pending[base.index] = 0
            continue
        if base.index not in completed:
            pending[base.index] = 0

    pending.update(
        {
            base.index: 0
            for base in race_schedule.groups
            if base.index not in completed and base.index not in pending
        }
    )
    while pending:
        round_items: list[tuple[RaceGroup, RaceGroup, int, str]] = []
        for base_index, attempt in sorted(pending.items()):
            if attempt >= MAX_RACE_ATTEMPTS:
                raise RaceModelError(f"race {base_index} failed after {MAX_RACE_ATTEMPTS} attempts")
            base = race_schedule.groups[base_index]
            candidate = _candidate_group(base, race_schedule.seed, attempt)
            race_id = _race_id(base.index, attempt)
            request = _race_request_payload(candidate, packs, mode_prompt, race_id)
            atomic_write_json(paths.race_request(race_id), request)
            round_items.append((base, candidate, attempt, race_id))

        console.print(f"Judging {len(round_items)} race(s)…")
        failures: dict[int, int] = {}
        workers = min(max(1, concurrency), len(round_items))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures: dict[Future[RaceResult], tuple[RaceGroup, RaceGroup, int, str]] = {}
            for item in round_items:
                base, candidate, attempt, race_id = item
                evidence = {ticker: packs[ticker] for ticker in candidate.tickers}
                allowed = {ticker: packs[ticker].allowed_evidence_ids for ticker in candidate.tickers}
                future = executor.submit(
                    judge.judge,
                    candidate.tickers,
                    evidence,
                    mode_prompt,
                    allowed,
                )
                futures[future] = (base, candidate, attempt, race_id)
            for future in as_completed(futures):
                base, _candidate, attempt, race_id = futures[future]
                try:
                    response = future.result()
                except Exception as exc:  # noqa: BLE001 - retry arbitrary provider failures
                    error: dict[str, Any] = {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "message": _safe_failure(exc),
                    }
                    raw_responses = getattr(exc, "raw_responses", ())
                    if (
                        isinstance(raw_responses, tuple | list)
                        and all(isinstance(item, str) for item in raw_responses)
                        and raw_responses
                    ):
                        error["raw_responses"] = list(raw_responses)
                    atomic_write_json(
                        paths.race_dir(race_id) / "error.json",
                        error,
                    )
                    failures[base.index] = attempt + 1
                    continue
                atomic_write_json(
                    paths.race_response(race_id),
                    response.model_dump(mode="json"),
                )
                completed[base.index] = response
        pending = failures
    return tuple(completed[group.index] for group in race_schedule.groups)


def _candidate_group(base: RaceGroup, seed: int, attempt: int) -> RaceGroup:
    return base if attempt == 0 else replacement_group(base, seed, attempt)


def _race_id(group_index: int, attempt: int) -> str:
    base = f"race-{group_index:04d}"
    return base if attempt == 0 else f"{base}-r{attempt}"


def _read_valid_race(
    paths: RunPaths,
    race_id: str,
    group: RaceGroup,
    packs: Mapping[str, EvidencePack],
    mode_prompt: str,
) -> RaceResult | None:
    path = paths.race_response(race_id)
    if not path.exists():
        return None
    try:
        expected_request = _race_request_payload(group, packs, mode_prompt, race_id)
        if read_json(paths.race_request(race_id)) != expected_request:
            return None
        raw = read_json(path)
        allowed = {ticker: packs[ticker].allowed_evidence_ids for ticker in group.tickers}
        return validate_race_result(
            json.dumps(raw, separators=(",", ":")),
            group.tickers,
            allowed,
        )
    except (KeyError, RunError, TypeError, ValueError):
        return None


def _race_request_payload(
    group: RaceGroup,
    packs: Mapping[str, EvidencePack],
    mode_prompt: str,
    race_id: str,
) -> dict[str, Any]:
    return {
        "race_id": race_id,
        "group_index": group.index,
        "replacement_for": group.replacement_for,
        "replacement_attempt": group.replacement_attempt,
        "tickers": list(group.tickers),
        "mode_prompt": mode_prompt,
        "allowed_evidence_ids": {
            ticker: list(packs[ticker].allowed_evidence_ids) for ticker in group.tickers
        },
        "evidence": {ticker: packs[ticker].as_prompt_dict() for ticker in group.tickers},
    }


def _massive_client(config: MassiveConfig) -> MassiveClient:
    return MassiveClient(
        require_environment(config.api_key_env),
        base_url=config.base_url,
    )


def _require_credentials(
    massive: MassiveConfig,
    sec: SecConfig,
    model: ModelConfig,
    *,
    massive_supplied: bool,
    sec_supplied: bool,
    judge_supplied: bool,
) -> None:
    if not massive_supplied:
        require_environment(massive.api_key_env)
    if not sec_supplied:
        require_environment(sec.user_agent_env)
    if not judge_supplied:
        require_environment(model.api_key_env)


def _sec_client(config: SecConfig) -> SecClient:
    return SecClient(
        require_environment(config.user_agent_env),
        base_url=config.base_url,
        rate_limit_per_second=config.requests_per_second,
    )


def _print_tracking(console: Console, report: TrackingReport) -> None:
    correlation = (
        "n/a" if report.spearman_rank_correlation is None else f"{report.spearman_rank_correlation:.4f}"
    )
    console.print(
        f"{report.run_id}: through {report.through.isoformat()}, "
        f"top-minus-bottom {report.top_minus_bottom:.4%}, "
        f"Spearman {correlation}"
    )


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ConfigError("timestamps must be timezone-aware")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_failure(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}"[:1000]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], value)


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RunError(f"{key} must be a non-empty string")
    return value.strip()


def _required_int(raw: Mapping[str, Any], key: str, minimum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunError(f"{key} must be an integer >= {minimum}")
    return value


def _required_float(raw: Mapping[str, Any], key: str, minimum: float) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not float(value) >= minimum:
        raise RunError(f"{key} must be a number >= {minimum:g}")
    return float(value)
