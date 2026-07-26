from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomli_w

ENGINE_VERSION = "1"
RUN_SCHEMA_VERSION = 2
PROFILE_NAMES = frozenset({"low", "medium", "high"})
DEFAULT_SEC_USER_AGENT = "stockrank/1.0 (https://github.com/kaufmann-dev/stockrank)"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_STARTER_FILES = (
    Path("stockrank.toml"),
    Path("modes/best-bet.toml"),
    Path("universes/liquid-50.toml"),
)


class ConfigError(RuntimeError):
    pass


def is_initialized(root: Path) -> bool:
    return (root / "stockrank.toml").is_file()


def initialize_project(root: Path) -> tuple[Path, ...]:
    targets = tuple(root / relative for relative in _STARTER_FILES)
    conflicts = [target for target in targets if target.exists()]
    if conflicts:
        rendered = ", ".join(str(path.relative_to(root)) for path in conflicts)
        raise ConfigError(f"cannot initialize because these files already exist: {rendered}")

    starter = files("stockrank").joinpath("starter")
    for relative, target in zip(_STARTER_FILES, targets, strict=True):
        resource = starter.joinpath(*relative.parts)
        raw = tomllib.loads(resource.read_text(encoding="utf-8"))
        write_toml(target, raw)
    return targets


def read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a TOML table")
    return value


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = tomli_w.dumps(data)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _table(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table")
    return raw


def _string(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return raw.strip()


def _integer(raw: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise ConfigError(f"{label} must be an integer >= {minimum}")
    return raw


def _number(raw: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) < minimum:
        raise ConfigError(f"{label} must be a number >= {minimum:g}")
    return float(raw)


@dataclass(frozen=True)
class MassiveConfig:
    base_url: str = "https://api.massive.com"
    concurrency: int = 4


@dataclass(frozen=True)
class SecConfig:
    user_agent: str = DEFAULT_SEC_USER_AGENT
    base_url: str = "https://data.sec.gov"
    requests_per_second: float = 8.0


@dataclass(frozen=True)
class DataConfig:
    price_history_days: int = 730
    liquidity_lookback_sessions: int = 20
    news_items: int = 20
    filing_items: int = 10
    insider_items: int = 20
    min_price_bars: int = 60
    evidence_char_budget: int = 12_000


@dataclass(frozen=True)
class ModelConfig:
    name: str
    base_url: str
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 2
    concurrency: int = 4
    max_tokens: int = 4096
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DefaultsConfig:
    model: str | None
    mode: str = "best-bet"
    profile: str = "medium"
    seed: int = 42


@dataclass(frozen=True)
class TrackingConfig:
    benchmark: str = "SPY"


@dataclass(frozen=True)
class AppConfig:
    massive: MassiveConfig
    sec: SecConfig
    data: DataConfig
    defaults: DefaultsConfig
    tracking: TrackingConfig
    models: dict[str, ModelConfig]


@dataclass(frozen=True)
class Mode:
    name: str
    rank_1_meaning: str
    prompt: str
    raw: dict[str, Any]


def load_app_config(root: Path) -> AppConfig:
    raw = read_toml(root / "stockrank.toml")
    sources = _table(raw.get("sources"), "sources")
    massive_raw = _table(sources.get("massive"), "sources.massive")
    sec_raw = _table(sources.get("sec"), "sources.sec")
    data_raw = _table(raw.get("data", {}), "data")
    defaults_raw = _table(raw.get("defaults"), "defaults")
    tracking_raw = _table(raw.get("tracking", {}), "tracking")
    models_raw = _table(raw.get("models"), "models")

    models: dict[str, ModelConfig] = {}
    for name, value in models_raw.items():
        model_raw = _table(value, f"models.{name}")
        declared_name = _string(name, "model profile name")
        if not _PROFILE_NAME_RE.fullmatch(declared_name):
            raise ConfigError(
                f"model profile name {declared_name!r} may contain only letters, numbers, '.', '_' and '-'"
            )
        unexpected = sorted(
            set(model_raw)
            - {
                "base_url",
                "model",
                "timeout_seconds",
                "max_retries",
                "concurrency",
                "max_tokens",
                "reasoning_effort",
                "extra_body",
            }
        )
        if unexpected:
            raise ConfigError(f"models.{name} contains unknown fields: {', '.join(unexpected)}")
        extra_body = model_raw.get("extra_body", {})
        if not isinstance(extra_body, dict):
            raise ConfigError(f"models.{name}.extra_body must be a table")
        base_url = _string(model_raw.get("base_url"), f"models.{name}.base_url").rstrip("/")
        _validate_http_url(base_url, f"models.{name}.base_url")
        models[name] = ModelConfig(
            name=declared_name,
            base_url=base_url,
            model=_string(model_raw.get("model"), f"models.{name}.model"),
            timeout_seconds=_number(
                model_raw.get("timeout_seconds", 120.0),
                f"models.{name}.timeout_seconds",
                minimum=0.1,
            ),
            max_retries=_integer(model_raw.get("max_retries", 2), f"models.{name}.max_retries"),
            concurrency=_integer(
                model_raw.get("concurrency", 4),
                f"models.{name}.concurrency",
                minimum=1,
            ),
            max_tokens=_integer(
                model_raw.get("max_tokens", 4096),
                f"models.{name}.max_tokens",
                minimum=1,
            ),
            reasoning_effort=(
                _string(
                    model_raw["reasoning_effort"],
                    f"models.{name}.reasoning_effort",
                )
                if model_raw.get("reasoning_effort") is not None
                else None
            ),
            extra_body=dict(extra_body),
        )
    default_model_raw = defaults_raw.get("model")
    if default_model_raw is None:
        if models:
            raise ConfigError("defaults.model is required when model profiles are configured")
        default_model = None
    else:
        default_model = _string(default_model_raw, "defaults.model")

    defaults = DefaultsConfig(
        model=default_model,
        mode=_string(defaults_raw.get("mode", "best-bet"), "defaults.mode"),
        profile=_string(defaults_raw.get("profile", "medium"), "defaults.profile"),
        seed=_integer(defaults_raw.get("seed", 42), "defaults.seed"),
    )
    if defaults.model is not None and defaults.model not in models:
        raise ConfigError(f"defaults.model references unknown profile {defaults.model!r}")
    if defaults.profile not in PROFILE_NAMES:
        raise ConfigError(f"defaults.profile must be one of {sorted(PROFILE_NAMES)}")

    massive_unexpected = sorted(set(massive_raw) - {"base_url", "concurrency"})
    if massive_unexpected:
        raise ConfigError("sources.massive contains unknown fields: " + ", ".join(massive_unexpected))
    sec_unexpected = sorted(set(sec_raw) - {"user_agent", "base_url", "requests_per_second"})
    if sec_unexpected:
        raise ConfigError("sources.sec contains unknown fields: " + ", ".join(sec_unexpected))

    return AppConfig(
        massive=MassiveConfig(
            base_url=_string(
                massive_raw.get("base_url", "https://api.massive.com"),
                "sources.massive.base_url",
            ).rstrip("/"),
            concurrency=_integer(
                massive_raw.get("concurrency", 4),
                "sources.massive.concurrency",
                minimum=1,
            ),
        ),
        sec=SecConfig(
            user_agent=_string(
                sec_raw.get("user_agent", DEFAULT_SEC_USER_AGENT),
                "sources.sec.user_agent",
            ),
            base_url=_string(
                sec_raw.get("base_url", "https://data.sec.gov"),
                "sources.sec.base_url",
            ).rstrip("/"),
            requests_per_second=_number(
                sec_raw.get("requests_per_second", 8.0),
                "sources.sec.requests_per_second",
                minimum=0.1,
            ),
        ),
        data=DataConfig(
            price_history_days=_integer(
                data_raw.get("price_history_days", 730),
                "data.price_history_days",
                minimum=60,
            ),
            liquidity_lookback_sessions=_integer(
                data_raw.get("liquidity_lookback_sessions", 20),
                "data.liquidity_lookback_sessions",
                minimum=1,
            ),
            news_items=_integer(data_raw.get("news_items", 20), "data.news_items"),
            filing_items=_integer(data_raw.get("filing_items", 10), "data.filing_items"),
            insider_items=_integer(data_raw.get("insider_items", 20), "data.insider_items"),
            min_price_bars=_integer(
                data_raw.get("min_price_bars", 60),
                "data.min_price_bars",
                minimum=2,
            ),
            evidence_char_budget=_integer(
                data_raw.get("evidence_char_budget", 12_000),
                "data.evidence_char_budget",
                minimum=1000,
            ),
        ),
        defaults=defaults,
        tracking=TrackingConfig(
            benchmark=_string(tracking_raw.get("benchmark", "SPY"), "tracking.benchmark").upper()
        ),
        models=models,
    )


def load_mode(root: Path, name: str) -> Mode:
    raw = read_toml(root / "modes" / f"{name}.toml")
    unexpected = sorted(set(raw) - {"name", "rank_1_meaning", "prompt"})
    if unexpected:
        raise ConfigError(f"mode file {name}.toml contains unknown fields: {', '.join(unexpected)}")
    declared = _string(raw.get("name"), f"mode {name}.name")
    if declared != name:
        raise ConfigError(f"mode file {name}.toml declares name {declared!r}")
    return Mode(
        name=declared,
        rank_1_meaning=_string(raw.get("rank_1_meaning"), f"mode {name}.rank_1_meaning"),
        prompt=_string(raw.get("prompt"), f"mode {name}.prompt"),
        raw=raw,
    )


def available_names(root: Path, folder: str) -> list[str]:
    path = root / folder
    if not path.exists():
        return []
    return sorted(file.stem for file in path.glob("*.toml") if file.is_file())


def model_config(root: Path, name: str | None = None) -> ModelConfig:
    config = load_app_config(root)
    selected = name or config.defaults.model
    if selected is None:
        raise ConfigError("no model profiles configured; add one with 'stockrank model add'")
    try:
        return config.models[selected]
    except KeyError as exc:
        raise ConfigError(f"unknown model profile {selected!r}") from exc


def profile_name(root: Path, requested: str | None = None) -> str:
    value = requested or load_app_config(root).defaults.profile
    if value not in PROFILE_NAMES:
        raise ConfigError(f"profile must be one of {sorted(PROFILE_NAMES)}")
    return value


def save_model_config(
    root: Path,
    profile: ModelConfig,
    *,
    make_default: bool = False,
    replace: bool = False,
) -> bool:
    entry = _model_entry(profile)
    raw = read_toml(root / "stockrank.toml")
    models = _table(raw.get("models"), "models")
    if profile.name in models and not replace:
        raise ConfigError(f"model profile {profile.name!r} already exists; use --replace to update it")
    made_default = make_default or not models
    models[profile.name] = entry
    if made_default:
        defaults = _table(raw.get("defaults"), "defaults")
        defaults["model"] = profile.name
    write_toml(root / "stockrank.toml", raw)
    load_app_config(root)
    return made_default


def delete_model_config(root: Path, name: str) -> None:
    raw = read_toml(root / "stockrank.toml")
    models = _table(raw.get("models"), "models")
    if name not in models:
        raise ConfigError(f"unknown model profile {name!r}")
    defaults = _table(raw.get("defaults"), "defaults")
    if defaults.get("model") == name:
        raise ConfigError(f"cannot remove default model profile {name!r}; make another profile default first")
    del models[name]
    write_toml(root / "stockrank.toml", raw)
    load_app_config(root)


def set_default_model(root: Path, name: str) -> None:
    raw = read_toml(root / "stockrank.toml")
    models = _table(raw.get("models"), "models")
    if name not in models:
        raise ConfigError(f"unknown model profile {name!r}")
    defaults = _table(raw.get("defaults"), "defaults")
    defaults["model"] = name
    write_toml(root / "stockrank.toml", raw)
    load_app_config(root)


def _validate_http_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{label} must be an absolute HTTP(S) URL")


def _model_entry(profile: ModelConfig) -> dict[str, Any]:
    if not _PROFILE_NAME_RE.fullmatch(profile.name):
        raise ConfigError("model profile name may contain only letters, numbers, '.', '_' and '-'")
    base_url = _string(profile.base_url, "model base_url").rstrip("/")
    _validate_http_url(base_url, "model base_url")
    entry: dict[str, Any] = {
        "base_url": base_url,
        "model": _string(profile.model, "model"),
        "timeout_seconds": _number(
            profile.timeout_seconds,
            "model timeout_seconds",
            minimum=0.1,
        ),
        "max_retries": _integer(profile.max_retries, "model max_retries"),
        "concurrency": _integer(
            profile.concurrency,
            "model concurrency",
            minimum=1,
        ),
        "max_tokens": _integer(
            profile.max_tokens,
            "model max_tokens",
            minimum=1,
        ),
    }
    if profile.reasoning_effort is not None:
        entry["reasoning_effort"] = _string(
            profile.reasoning_effort,
            "reasoning_effort",
        )
    if not isinstance(profile.extra_body, dict):
        raise ConfigError("model extra_body must be a table")
    if profile.extra_body:
        entry["extra_body"] = dict(profile.extra_body)
    return entry
