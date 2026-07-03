from __future__ import annotations

import csv
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from .aggregate import parse_portfolio_doc
from .config import ConfigError, latest_run, load_run_manifest, read_toml, score_mode_summary, write_toml


def commit_run(
    root: Path,
    *,
    run_id: str | None = None,
    confirm: bool | None = None,
    console: Console | None = None,
    now: datetime | None = None,
) -> str:
    console = console or Console()
    selected = run_id or latest_run(root, "portfolio")
    run_root = root / "runs" / selected
    if not all((run_root / "portfolio" / name).exists() for name in ("portfolio.csv", "trades.csv", "report.md")):
        raise ConfigError(f"run {selected} does not have a finalized portfolio")
    live_path = root / "live" / "portfolio.toml"
    if live_path.exists() and live_path.stat().st_size > 0:
        live = read_toml(live_path)
        if str(live.get("run_id")) == selected:
            console.print(f"[yellow]warning:[/] run {selected} is already committed")
    _print_trades(console, run_root / "portfolio" / "trades.csv")
    turnover = _turnover(run_root / "portfolio" / "trades.csv")
    console.print(f"Implied turnover: {turnover:.6f}")
    if confirm is None:
        confirm = Confirm.ask(f"Commit run {selected} as the live portfolio?", default=False)
    if not confirm:
        raise ConfigError("commit cancelled")

    committed = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if live_path.exists() and live_path.stat().st_size > 0:
        history = root / "live" / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = committed.replace("-", "").replace(":", "").replace("+00:00", "Z")
        shutil.copy2(live_path, history / f"{stamp}.toml")

    manifest = load_run_manifest(root, selected)
    doc, warnings = parse_portfolio_doc(run_root / "portfolio" / "work" / "portfolio.toml", proposal=False)
    if doc is None:
        raise ConfigError(f"cannot read finalized portfolio thesis: {'; '.join(warnings)}")
    positions = _portfolio_rows(run_root / "portfolio" / "portfolio.csv")
    payload: dict[str, Any] = {
        "run_id": selected,
        "committed": committed,
        "strategy": manifest.get("strategy", {}).get("name", ""),
        "mode_summary": score_mode_summary(manifest),
        "summary": doc.summary,
        "positions": positions,
    }
    write_toml(live_path, payload)
    console.print(f"Committed run {selected} to live/portfolio.toml")
    return selected


def _portfolio_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            {"ticker": row["ticker"], "weight": float(row["weight"]), "rationale": row["rationale"]}
            for row in csv.DictReader(fh)
        ]


def _turnover(path: Path) -> float:
    with path.open(newline="", encoding="utf-8") as fh:
        return 0.5 * sum(abs(float(row["delta"])) for row in csv.DictReader(fh))


def _print_trades(console: Console, path: Path) -> None:
    table = Table(title="Proposed Trades")
    for column in ("ticker", "current_weight", "target_weight", "delta"):
        table.add_column(column)
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            table.add_row(row["ticker"], row["current_weight"], row["target_weight"], row["delta"])
    console.print(table)
