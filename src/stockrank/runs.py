from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import tomli_w

from .config import ENGINE_VERSION, RUN_SCHEMA_VERSION

RunStatus = Literal["running", "completed", "failed"]
ArtifactValidator = Callable[[Any], bool]
_RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{12}Z$")
_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUN_STATUSES = frozenset({"running", "completed", "failed"})


class RunError(RuntimeError):
    """Raised when run state or an artifact is invalid."""


class IncompatibleRunError(RunError):
    """Raised when a run cannot be resumed by this engine."""


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RunError(f"cannot serialize JSON artifact {path}: {exc}") from exc
    atomic_write_text(path, f"{rendered}\n")


def atomic_write_toml(path: Path, value: Mapping[str, Any]) -> None:
    try:
        rendered = tomli_w.dumps(dict(value))
    except (TypeError, ValueError) as exc:
        raise RunError(f"cannot serialize TOML artifact {path}: {exc}") from exc
    atomic_write_text(path, rendered)


def atomic_write_csv(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> None:
    if not fieldnames or len(fieldnames) != len(set(fieldnames)):
        raise RunError("CSV fieldnames must be non-empty and unique")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({name: "" if row.get(name) is None else row.get(name) for name in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except (csv.Error, ValueError) as exc:
        raise RunError(f"cannot serialize CSV artifact {path}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard numeric constant {value}")

    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle, parse_constant=reject_nonstandard_constant)
    except FileNotFoundError as exc:
        raise RunError(f"missing JSON artifact: {path}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise RunError(f"invalid JSON artifact {path}: {exc}") from exc


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RunError(f"missing TOML artifact: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RunError(f"invalid TOML artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"TOML artifact must contain a table: {path}")
    return value


def read_csv(path: Path, *, required_fields: Iterable[str] = ()) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, strict=True)
            fields = reader.fieldnames
            if fields is None:
                raise RunError(f"CSV artifact has no header: {path}")
            missing = set(required_fields) - set(fields)
            if missing:
                raise RunError(f"CSV artifact {path} is missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
            if any(None in row for row in rows):
                raise RunError(f"CSV artifact has rows wider than its header: {path}")
            return rows
    except FileNotFoundError as exc:
        raise RunError(f"missing CSV artifact: {path}") from exc
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RunError(f"invalid CSV artifact {path}: {exc}") from exc


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise RunError("timestamps must be timezone-aware")
    return current.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RunError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RunError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def make_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise RunError("run id timestamp must be timezone-aware")
    return current.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _PATH_SEGMENT_PATTERN.fullmatch(value):
        raise RunError(f"{label} must contain only letters, numbers, '.', '_' or '-'")
    return value


def _json_copy(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        rendered = json.dumps(value, allow_nan=False)
        copied = json.loads(rendered)
    except (TypeError, ValueError) as exc:
        raise RunError(f"{label} must be JSON-serializable: {exc}") from exc
    if not isinstance(copied, dict):
        raise RunError(f"{label} must be an object")
    return copied


@dataclass(frozen=True)
class LifecycleEvent:
    event: str
    at: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {"event": self.event, "at": self.at}
        if self.detail is not None:
            value["detail"] = self.detail
        return value

    @classmethod
    def from_dict(cls, raw: Any) -> LifecycleEvent:
        if not isinstance(raw, dict):
            raise RunError("manifest lifecycle entries must be objects")
        event = raw.get("event")
        at = raw.get("at")
        detail = raw.get("detail")
        if not isinstance(event, str) or not event:
            raise RunError("manifest lifecycle event must be a non-empty string")
        if not isinstance(at, str):
            raise RunError("manifest lifecycle event timestamp must be a string")
        _parse_timestamp(at, "manifest lifecycle event timestamp")
        if detail is not None and not isinstance(detail, str):
            raise RunError("manifest lifecycle detail must be a string")
        return cls(event=event, at=at, detail=detail)


@dataclass(frozen=True)
class RunManifest:
    id: str
    schema_version: int
    engine_version: str
    created_at: str
    updated_at: str
    status: RunStatus
    universe: dict[str, Any]
    mode: dict[str, Any]
    model: dict[str, Any]
    settings: dict[str, Any]
    profile: str
    seed: int
    lifecycle: tuple[LifecycleEvent, ...]
    completed_at: str | None = None
    tracking_inception: str | None = None
    failure: str | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        universe: Mapping[str, Any],
        mode: Mapping[str, Any],
        model: Mapping[str, Any],
        settings: Mapping[str, Any],
        profile: str,
        seed: int,
        now: datetime | None = None,
    ) -> RunManifest:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise RunError(f"invalid timestamp run id: {run_id!r}")
        if not isinstance(profile, str) or not profile.strip():
            raise RunError("profile must be a non-empty string")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise RunError("seed must be an integer")
        at = _timestamp(now)
        return cls(
            id=run_id,
            schema_version=RUN_SCHEMA_VERSION,
            engine_version=ENGINE_VERSION,
            created_at=at,
            updated_at=at,
            status="running",
            universe=_json_copy(universe, "universe"),
            mode=_json_copy(mode, "mode"),
            model=_json_copy(model, "model"),
            settings=_json_copy(settings, "settings"),
            profile=profile.strip(),
            seed=seed,
            lifecycle=(LifecycleEvent("created", at),),
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "schema_version": self.schema_version,
            "engine_version": self.engine_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "universe": self.universe,
            "mode": self.mode,
            "model": self.model,
            "settings": self.settings,
            "profile": self.profile,
            "seed": self.seed,
            "lifecycle": [event.to_dict() for event in self.lifecycle],
        }
        if self.completed_at is not None:
            value["completed_at"] = self.completed_at
        if self.tracking_inception is not None:
            value["tracking_inception"] = self.tracking_inception
        if self.failure is not None:
            value["failure"] = self.failure
        return value

    @classmethod
    def from_dict(cls, raw: Any) -> RunManifest:
        if not isinstance(raw, dict):
            raise RunError("manifest must be a JSON object")
        run_id = raw.get("id")
        schema_version = raw.get("schema_version")
        engine_version = raw.get("engine_version")
        created_at = raw.get("created_at")
        updated_at = raw.get("updated_at")
        status = raw.get("status")
        profile = raw.get("profile")
        seed = raw.get("seed")
        lifecycle = raw.get("lifecycle")
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
            raise RunError("manifest id must be a timestamp run id")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise RunError("manifest schema_version must be an integer")
        if not isinstance(engine_version, str) or not engine_version:
            raise RunError("manifest engine_version must be a non-empty string")
        if not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise RunError("manifest created_at and updated_at must be strings")
        _parse_timestamp(created_at, "manifest created_at")
        _parse_timestamp(updated_at, "manifest updated_at")
        if status not in _RUN_STATUSES:
            raise RunError(f"manifest status must be one of {sorted(_RUN_STATUSES)}")
        if not isinstance(profile, str) or not profile:
            raise RunError("manifest profile must be a non-empty string")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise RunError("manifest seed must be an integer")
        if not isinstance(lifecycle, list) or not lifecycle:
            raise RunError("manifest lifecycle must be a non-empty list")
        completed_at = raw.get("completed_at")
        if completed_at is not None:
            if not isinstance(completed_at, str):
                raise RunError("manifest completed_at must be a string")
            _parse_timestamp(completed_at, "manifest completed_at")
        if status == "completed" and completed_at is None:
            raise RunError("completed manifest must have completed_at")
        tracking_inception = raw.get("tracking_inception")
        if tracking_inception is not None:
            if not isinstance(tracking_inception, str):
                raise RunError("manifest tracking_inception must be a date string")
            try:
                date.fromisoformat(tracking_inception)
            except ValueError as exc:
                raise RunError("manifest tracking_inception must be an ISO date") from exc
        failure = raw.get("failure")
        if failure is not None and not isinstance(failure, str):
            raise RunError("manifest failure must be a string")
        return cls(
            id=run_id,
            schema_version=schema_version,
            engine_version=engine_version,
            created_at=created_at,
            updated_at=updated_at,
            status=status,
            universe=_required_object(raw.get("universe"), "manifest universe"),
            mode=_required_object(raw.get("mode"), "manifest mode"),
            model=_required_object(raw.get("model"), "manifest model"),
            settings=_required_object(raw.get("settings"), "manifest settings"),
            profile=profile,
            seed=seed,
            lifecycle=tuple(LifecycleEvent.from_dict(event) for event in lifecycle),
            completed_at=completed_at,
            tracking_inception=tracking_inception,
            failure=failure,
        )


def _required_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunError(f"{label} must be an object")
    return _json_copy(value, label)


@dataclass(frozen=True)
class RunPaths:
    root: Path
    run_id: str

    @property
    def run(self) -> Path:
        return self.root / "runs" / self.run_id

    @property
    def manifest(self) -> Path:
        return self.run / "manifest.json"

    @property
    def universe(self) -> Path:
        return self.run / "universe.json"

    @property
    def raw(self) -> Path:
        return self.run / "raw"

    def raw_source(self, provider: str, ticker: str, name: str, suffix: str = "json") -> Path:
        provider = _segment(provider.lower(), "provider")
        ticker = _segment(ticker.upper(), "ticker")
        name = _segment(name, "source name")
        suffix = _segment(suffix.removeprefix("."), "source suffix")
        return self.raw / provider / ticker / f"{name}.{suffix}"

    @property
    def evidence_dir(self) -> Path:
        return self.run / "evidence"

    def evidence(self, ticker: str) -> Path:
        return self.evidence_dir / f"{_segment(ticker.upper(), 'ticker')}.json"

    @property
    def schedule(self) -> Path:
        return self.run / "schedule.json"

    @property
    def races_dir(self) -> Path:
        return self.run / "races"

    def race_dir(self, race_id: str) -> Path:
        return self.races_dir / _segment(race_id, "race id")

    def race_request(self, race_id: str) -> Path:
        return self.race_dir(race_id) / "request.json"

    def race_response(self, race_id: str) -> Path:
        return self.race_dir(race_id) / "response.json"

    @property
    def results(self) -> Path:
        return self.run / "results"

    @property
    def ranking_csv(self) -> Path:
        return self.results / "ranking.csv"

    @property
    def ranking_json(self) -> Path:
        return self.results / "ranking.json"

    @property
    def report(self) -> Path:
        return self.results / "report.md"

    @property
    def performance_csv(self) -> Path:
        return self.results / "performance.csv"

    @property
    def performance_json(self) -> Path:
        return self.results / "performance.json"


@dataclass(frozen=True)
class SourceArtifact:
    provider: str
    ticker: str
    name: str
    suffix: str = "json"

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.ticker}/{self.name}.{self.suffix.removeprefix('.')}"


@dataclass(frozen=True)
class ResumeState:
    manifest: RunManifest
    valid_sources: tuple[SourceArtifact, ...]
    pending_sources: tuple[SourceArtifact, ...]
    valid_evidence: tuple[str, ...]
    pending_evidence: tuple[str, ...]
    valid_races: tuple[str, ...]
    pending_races: tuple[str, ...]


def _valid_artifact(path: Path, validator: ArtifactValidator | None = None) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        suffix = path.suffix.lower()
        if suffix == ".json":
            value = read_json(path)
        elif suffix == ".toml":
            value = read_toml(path)
        elif suffix == ".csv":
            value = read_csv(path)
        else:
            value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, RunError):
        return False
    if validator is None:
        return not isinstance(value, str) or bool(value.strip())
    try:
        return bool(validator(value))
    except (KeyError, TypeError, ValueError):
        return False


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.runs_root = self.root / "runs"

    def paths(self, run_id: str) -> RunPaths:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise RunError(f"invalid timestamp run id: {run_id!r}")
        return RunPaths(self.root, run_id)

    def create(
        self,
        *,
        universe: Mapping[str, Any],
        mode: Mapping[str, Any],
        model: Mapping[str, Any],
        settings: Mapping[str, Any],
        profile: str,
        seed: int,
        now: datetime | None = None,
    ) -> RunManifest:
        run_id = make_run_id(now)
        paths = self.paths(run_id)
        try:
            paths.run.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RunError(f"run already exists: {run_id}") from exc
        manifest = RunManifest.create(
            run_id=run_id,
            universe=universe,
            mode=mode,
            model=model,
            settings=settings,
            profile=profile,
            seed=seed,
            now=now,
        )
        atomic_write_json(paths.universe, manifest.universe)
        self._save(manifest)
        return manifest

    def load(self, run_id: str) -> RunManifest:
        paths = self.paths(run_id)
        manifest = RunManifest.from_dict(read_json(paths.manifest))
        if manifest.id != run_id:
            raise RunError(f"manifest id {manifest.id!r} does not match run directory {run_id!r}")
        return manifest

    def list(self, *, status: RunStatus | None = None) -> list[RunManifest]:
        if status is not None and status not in _RUN_STATUSES:
            raise RunError(f"run status must be one of {sorted(_RUN_STATUSES)}")
        if not self.runs_root.exists():
            return []
        manifests = [
            self.load(path.name)
            for path in self.runs_root.iterdir()
            if path.is_dir() and _RUN_ID_PATTERN.fullmatch(path.name) and (path / "manifest.json").is_file()
        ]
        if status is not None:
            manifests = [manifest for manifest in manifests if manifest.status == status]
        return sorted(manifests, key=lambda manifest: (manifest.created_at, manifest.id), reverse=True)

    def select(self, run_id: str | None = None, *, status: RunStatus | None = None) -> RunManifest:
        if run_id is not None:
            manifest = self.load(run_id)
            if status is not None and manifest.status != status:
                raise RunError(f"run {run_id} has status {manifest.status!r}, expected {status!r}")
            return manifest
        candidates = self.list(status=status)
        if not candidates:
            qualifier = f" with status {status!r}" if status is not None else ""
            raise RunError(f"no runs found{qualifier}")
        return candidates[0]

    def complete(self, run_id: str, *, now: datetime | None = None) -> RunManifest:
        manifest = self.load(run_id)
        if manifest.status == "completed":
            return manifest
        paths = self.paths(run_id)
        required = (
            (paths.ranking_csv, _valid_ranking_csv),
            (paths.ranking_json, _valid_ranking_json),
            (paths.report, None),
        )
        invalid = [
            str(path.relative_to(paths.run))
            for path, validator in required
            if not _valid_artifact(path, validator)
        ]
        if invalid:
            raise RunError(
                f"cannot complete run {run_id}: missing or invalid artifacts: " + ", ".join(invalid)
            )
        at = _timestamp(now)
        completed = replace(
            manifest,
            status="completed",
            updated_at=at,
            completed_at=at,
            failure=None,
            lifecycle=(*manifest.lifecycle, LifecycleEvent("completed", at)),
        )
        self._save(completed)
        return completed

    def fail(self, run_id: str, detail: str, *, now: datetime | None = None) -> RunManifest:
        if not isinstance(detail, str) or not detail.strip():
            raise RunError("failure detail must be a non-empty string")
        manifest = self.load(run_id)
        if manifest.status == "completed":
            raise RunError(f"completed run {run_id} cannot be marked failed")
        at = _timestamp(now)
        failed = replace(
            manifest,
            status="failed",
            updated_at=at,
            failure=detail.strip(),
            lifecycle=(*manifest.lifecycle, LifecycleEvent("failed", at, detail.strip())),
        )
        self._save(failed)
        return failed

    def resume(
        self,
        run_id: str,
        *,
        sources: Iterable[SourceArtifact] = (),
        evidence_tickers: Iterable[str] = (),
        race_ids: Iterable[str] = (),
        source_validator: ArtifactValidator | None = None,
        evidence_validator: ArtifactValidator | None = None,
        race_validator: ArtifactValidator | None = None,
        now: datetime | None = None,
    ) -> ResumeState:
        manifest = self.load(run_id)
        self._validate_resume_version(manifest)
        if manifest.status == "completed":
            raise RunError(f"completed run {run_id} cannot be resumed")
        paths = self.paths(run_id)
        source_items = tuple(sources)
        evidence_items = tuple(_segment(ticker.upper(), "ticker") for ticker in evidence_tickers)
        race_items = tuple(_segment(race_id, "race id") for race_id in race_ids)
        valid_sources = tuple(
            source
            for source in source_items
            if _valid_artifact(
                paths.raw_source(source.provider, source.ticker, source.name, source.suffix),
                source_validator,
            )
        )
        valid_evidence = tuple(
            ticker for ticker in evidence_items if _valid_artifact(paths.evidence(ticker), evidence_validator)
        )
        valid_races = tuple(
            race_id for race_id in race_items if _valid_artifact(paths.race_response(race_id), race_validator)
        )
        valid_source_set = set(valid_sources)
        valid_evidence_set = set(valid_evidence)
        valid_race_set = set(valid_races)
        at = _timestamp(now)
        resumed = replace(
            manifest,
            status="running",
            updated_at=at,
            failure=None,
            lifecycle=(*manifest.lifecycle, LifecycleEvent("resumed", at)),
        )
        self._save(resumed)
        return ResumeState(
            manifest=resumed,
            valid_sources=valid_sources,
            pending_sources=tuple(source for source in source_items if source not in valid_source_set),
            valid_evidence=valid_evidence,
            pending_evidence=tuple(ticker for ticker in evidence_items if ticker not in valid_evidence_set),
            valid_races=valid_races,
            pending_races=tuple(race_id for race_id in race_items if race_id not in valid_race_set),
        )

    def set_tracking_inception(
        self, run_id: str, inception: date, *, now: datetime | None = None
    ) -> RunManifest:
        manifest = self.load(run_id)
        if manifest.status != "completed":
            raise RunError(f"run {run_id} must be completed before tracking")
        value = inception.isoformat()
        if manifest.tracking_inception is not None:
            if manifest.tracking_inception != value:
                raise RunError(
                    f"run {run_id} tracking inception is already {manifest.tracking_inception}, not {value}"
                )
            return manifest
        at = _timestamp(now)
        updated = replace(
            manifest,
            updated_at=at,
            tracking_inception=value,
            lifecycle=(*manifest.lifecycle, LifecycleEvent("tracking_started", at, value)),
        )
        self._save(updated)
        return updated

    def _save(self, manifest: RunManifest) -> None:
        atomic_write_json(self.paths(manifest.id).manifest, manifest.to_dict())

    @staticmethod
    def _validate_resume_version(manifest: RunManifest) -> None:
        if manifest.schema_version != RUN_SCHEMA_VERSION:
            raise IncompatibleRunError(
                f"run {manifest.id} uses schema {manifest.schema_version}; this engine requires {RUN_SCHEMA_VERSION}"
            )
        if manifest.engine_version != ENGINE_VERSION:
            raise IncompatibleRunError(
                f"run {manifest.id} uses engine {manifest.engine_version!r}; "
                f"this engine requires {ENGINE_VERSION!r}"
            )


def _valid_ranking_csv(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(row, dict)
        and isinstance(row.get("ticker"), str)
        and bool(row["ticker"].strip())
        and isinstance(row.get("rank"), str)
        and row["rank"].isdigit()
        for row in value
    )


def _valid_ranking_json(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    ranking = value.get("ranking")
    return (
        isinstance(ranking, list)
        and bool(ranking)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("ticker"), str)
            and bool(row["ticker"].strip())
            and isinstance(row.get("rank"), int)
            and not isinstance(row.get("rank"), bool)
            for row in ranking
        )
    )


def validate_finite_number(value: object, label: str) -> float:
    """Validate numeric artifact values without accepting booleans or NaN."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RunError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise RunError(f"{label} must be finite")
    return number
