from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from rich.console import Console

from .config import (
    ConfigError,
    DataConfig,
    build_run_manifest,
    latest_run,
    load_global_config,
    load_run_manifest,
    mode_snapshot,
    portfolio_pair_from_manifest,
    proposal_pairs_from_manifest,
    resolve_strategy,
    score_modes_from_manifest,
    write_toml,
)
from .massive import MassiveClient


class DataClient(Protocol):
    def fetch_many(self, tickers: list[str], data_config: DataConfig) -> dict[str, dict[str, str]]:
        ...


def render_prompt(root: Path, template_name: str, **values: object) -> str:
    template = (root / "prompts" / template_name).read_text()
    return template.format(**values)


def create_run_id(root: Path, now: datetime | None = None) -> int:
    current = int((now or datetime.now(UTC)).timestamp())
    while (root / "runs" / str(current)).exists():
        current += 1
    return current


def _reset_work(path: Path, force: bool) -> None:
    if path.exists():
        if not force:
            raise ConfigError(f"{path} already exists; pass --force to overwrite the phase work folder")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _write_bundle(data_dir: Path, bundle: dict[str, str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in bundle.items():
        (data_dir / filename).write_text(content, encoding="utf-8")


def prepare_scores(
    root: Path,
    strategy_name: str,
    *,
    force: bool = False,
    client: DataClient | None = None,
    console: Console | None = None,
    now: datetime | None = None,
) -> str:
    console = console or Console()
    global_config = load_global_config(root)
    resolved = resolve_strategy(root, strategy_name)
    console.print(f"Planned score evaluations: {resolved.planned_score_evaluations}")
    run_id = create_run_id(root, now)
    run_root = root / "runs" / str(run_id)
    work_root = run_root / "scores" / "work"
    _reset_work(work_root, force)

    created = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    write_toml(run_root / "run.toml", build_run_manifest(resolved, run_id, created))

    data_client = client or MassiveClient.from_env(root)
    bundles = data_client.fetch_many(resolved.universe.tickers, global_config.data)
    for score_mode in resolved.strategy.score_modes:
        mode = resolved.modes[score_mode.name]
        for harness in score_mode.harnesses:
            harness_root = work_root / score_mode.name / harness.name
            for ticker in resolved.universe.tickers:
                ticker_root = harness_root / ticker
                _write_bundle(ticker_root / "data", bundles[ticker])
                task = render_prompt(
                    root,
                    "scores.txt",
                    persona=mode.persona,
                    task=mode.task,
                    ticker=ticker,
                    mode=score_mode.name,
                    universe=resolved.universe.name,
                    workdir=ticker,
                )
                (ticker_root / "task.txt").write_text(task, encoding="utf-8")
    console.print(f"Created run {run_id}")
    return str(run_id)


def prepare_proposals(root: Path, *, run_id: str | None = None, force: bool = False) -> str:
    selected = run_id or latest_run(root, "scores")
    run_root = root / "runs" / selected
    if not all((run_root / "scores" / name).exists() for name in ("scores.csv", "rationales.csv", "report.md")):
        raise ConfigError(f"run {selected} does not have finalized scores")
    manifest = load_run_manifest(root, selected)
    work_root = run_root / "proposals" / "work"
    _reset_work(work_root, force)
    for pair in proposal_pairs_from_manifest(manifest):
        folder = work_root / f"{pair.mode}__{pair.harness}"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("scores.csv", "rationales.csv", "report.md"):
            shutil.copy2(run_root / "scores" / name, folder / name)
        mode = mode_snapshot(manifest, pair.mode)
        task = render_prompt(
            root,
            "proposals.txt",
            persona=mode["persona"],
            task=mode["task"],
            universe=manifest.get("universe_name", manifest.get("strategy", {}).get("universe", "")),
            num_positions_hint=manifest.get("strategy", {}).get("proposals", {}).get("num_positions", ""),
            workdir=".",
        )
        (folder / "task.txt").write_text(task, encoding="utf-8")
    return selected


def render_live_current(root: Path, target: Path) -> None:
    live_path = root / "live" / "portfolio.toml"
    target.mkdir(parents=True, exist_ok=True)
    csv_path = target / "current.csv"
    md_path = target / "current.md"
    if not live_path.exists() or live_path.stat().st_size == 0:
        csv_path.write_text("ticker,weight,rationale\n", encoding="utf-8")
        md_path.write_text("# Current Live Portfolio\n\nNo live portfolio is committed. Treat the current book as 100% cash.\n", encoding="utf-8")
        return
    from .config import read_toml

    live = read_toml(live_path)
    lines = ["ticker,weight,rationale"]
    for position in live.get("positions", []) or []:
        rationale = str(position.get("rationale", "")).replace('"', '""')
        lines.append(f'{position.get("ticker","")},{position.get("weight",0)},"{rationale}"')
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    md = [
        "# Current Live Portfolio",
        "",
        f"Run: {live.get('run_id', '')}",
        f"Committed: {live.get('committed', '')}",
        f"Strategy: {live.get('strategy', '')}",
        f"Mode weights: {live.get('mode_summary', '')}",
        "",
        "## Thesis",
        "",
        str(live.get("summary", "")),
        "",
        "## Positions",
        "",
    ]
    if live.get("positions"):
        md.extend(["| ticker | weight | rationale |", "|---|---:|---|"])
        for position in live.get("positions", []):
            md.append(f"| {position.get('ticker', '')} | {float(position.get('weight', 0)):.6f} | {position.get('rationale', '')} |")
    else:
        md.append("No committed positions.")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")


def prepare_portfolio(root: Path, *, run_id: str | None = None, force: bool = False) -> str:
    selected = run_id or latest_run(root, "proposals")
    run_root = root / "runs" / selected
    if not all((run_root / "proposals" / name).exists() for name in ("proposals.csv", "proposals.md")):
        raise ConfigError(f"run {selected} does not have finalized proposals")
    manifest = load_run_manifest(root, selected)
    work_root = run_root / "portfolio" / "work"
    _reset_work(work_root, force)
    for name in ("scores.csv", "rationales.csv", "report.md"):
        shutil.copy2(run_root / "scores" / name, work_root / name)
    for name in ("proposals.csv", "proposals.md"):
        shutil.copy2(run_root / "proposals" / name, work_root / name)
    render_live_current(root, work_root)
    pair = portfolio_pair_from_manifest(manifest)
    mode = mode_snapshot(manifest, pair.mode)
    task = render_prompt(
        root,
        "portfolio.txt",
        persona=mode["persona"],
        task=mode["task"],
        workdir=".",
    )
    (work_root / "task.txt").write_text(task, encoding="utf-8")
    return selected


def expected_score_pairs(manifest: dict[str, object]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for mode in score_modes_from_manifest(manifest):
        for harness in mode.harnesses:
            pairs.append((mode.name, harness.name))
    return pairs
