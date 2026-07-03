from __future__ import annotations

import csv
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console

from .aggregate import PortfolioDoc, Position, normalize_positions, parse_portfolio_doc, parse_score_result
from .config import (
    ConfigError,
    load_run_manifest,
    proposal_pairs_from_manifest,
    score_mode_summary,
    score_modes_from_manifest,
    update_run_manifest,
)


def _selected_run(root: Path, run_id: str | None, phase: str) -> str:
    from .config import latest_run

    return run_id or latest_run(root)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6f}"
    return str(value)


def finalize_scores(root: Path, *, run_id: str | None = None, console: Console | None = None) -> str:
    selected = _selected_run(root, run_id, "scores")
    console = console or Console()
    manifest = load_run_manifest(root, selected)
    run_root = root / "runs" / selected
    tickers = [str(t).upper() for t in manifest.get("tickers", [])]
    score_modes = score_modes_from_manifest(manifest)
    raw_columns = [f"{mode.name}__{harness.name}" for mode in score_modes for harness in mode.harnesses]
    mode_columns = [f"{mode.name}__score" for mode in score_modes]
    score_rows: list[dict[str, Any]] = []
    rationale_rows: list[dict[str, Any]] = []
    issues: list[str] = []

    for ticker in tickers:
        row: dict[str, Any] = {"ticker": ticker}
        mode_aggregates: dict[str, float] = {}
        for mode in score_modes:
            valid_scores: list[tuple[float, float]] = []
            for harness in mode.harnesses:
                column = f"{mode.name}__{harness.name}"
                path = run_root / "scores" / "work" / mode.name / harness.name / ticker / "results.toml"
                result, error = parse_score_result(path, ticker)
                if error:
                    row[column] = math.nan
                    issues.append(f"{ticker} {mode.name}__{harness.name}: {error}")
                    continue
                assert result is not None
                row[column] = result.score
                valid_scores.append((harness.weight, result.score))
                rationale_rows.append(
                    {
                        "ticker": ticker,
                        "mode": mode.name,
                        "harness": harness.name,
                        "score": _fmt(result.score),
                        "confidence": _fmt(result.confidence),
                        "summary": result.summary,
                    }
                )
            if valid_scores:
                weight_sum = sum(weight for weight, _ in valid_scores)
                mode_aggregates[mode.name] = sum((weight / weight_sum) * score for weight, score in valid_scores)
                row[f"{mode.name}__score"] = mode_aggregates[mode.name]
            else:
                row[f"{mode.name}__score"] = math.nan
        valid_modes = [(mode.weight, mode_aggregates[mode.name]) for mode in score_modes if mode.name in mode_aggregates]
        if valid_modes:
            weight_sum = sum(weight for weight, _ in valid_modes)
            row["final_score"] = sum((weight / weight_sum) * score for weight, score in valid_modes)
        else:
            row["final_score"] = math.nan
        score_rows.append(row)

    ranked = sorted(score_rows, key=lambda row: (-row["final_score"] if not math.isnan(row["final_score"]) else math.inf, row["ticker"]))
    rank = 1
    for row in ranked:
        if math.isnan(row["final_score"]):
            row["rank"] = ""
        else:
            row["rank"] = rank
            rank += 1
    output_rows = [{key: _fmt(row.get(key, "")) for key in ["ticker", *raw_columns, *mode_columns, "final_score", "rank"]} for row in ranked]
    _write_csv(run_root / "scores" / "scores.csv", ["ticker", *raw_columns, *mode_columns, "final_score", "rank"], output_rows)
    _write_csv(run_root / "scores" / "rationales.csv", ["ticker", "mode", "harness", "score", "confidence", "summary"], rationale_rows)
    (run_root / "scores" / "report.md").write_text(_score_report(manifest, ranked, score_modes, issues), encoding="utf-8")
    for issue in issues:
        console.print(f"[yellow]warning:[/] {issue}")
    return selected


def _score_report(manifest: dict[str, Any], rows: list[dict[str, Any]], score_modes: list[Any], issues: list[str]) -> str:
    lines = [
        "# Score Finalization Report",
        "",
        f"Run: {manifest.get('id', '')}",
        f"Created: {manifest.get('created', '')}",
        f"Universe: {manifest.get('universe_name', manifest.get('strategy', {}).get('universe', ''))}",
        f"Strategy: {manifest.get('strategy', {}).get('name', '')}",
        f"Mode weights: {score_mode_summary(manifest)}",
        "",
        "## Harness Matrix",
        "",
    ]
    for mode in score_modes:
        harnesses = ", ".join(f"{h.name}x{h.weight:g}" for h in mode.harnesses)
        lines.append(f"- {mode.name} x {mode.weight:g}: {harnesses}")
    lines.extend(["", "## Top 25", "", "| rank | ticker | final_score |", "|---:|---|---:|"])
    for row in rows[:25]:
        if math.isnan(row["final_score"]):
            continue
        lines.append(f"| {row['rank']} | {row['ticker']} | {row['final_score']:.6f} |")
    lines.append("")
    for mode in score_modes:
        column = f"{mode.name}__score"
        mode_rows = [row for row in rows if not math.isnan(row.get(column, math.nan))]
        mode_rows = sorted(mode_rows, key=lambda row: (-row[column], row["ticker"]))[:10]
        lines.extend([f"## Top 10: {mode.name}", "", "| ticker | score |", "|---|---:|"])
        for row in mode_rows:
            lines.append(f"| {row['ticker']} | {row[column]:.6f} |")
        lines.append("")
    lines.extend(["## Missing Or Invalid Results", ""])
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def finalize_proposals(root: Path, *, run_id: str | None = None, console: Console | None = None) -> str:
    selected = _selected_run(root, run_id, "proposals")
    console = console or Console()
    manifest = load_run_manifest(root, selected)
    run_root = root / "runs" / selected
    if not (run_root / "scores" / "scores.csv").exists():
        raise ConfigError(f"run {selected} does not have finalized scores")
    universe = set(str(t).upper() for t in manifest.get("tickers", []))
    rows: list[dict[str, Any]] = []
    sections: list[str] = ["# Proposal Finalization Report", ""]
    warnings: list[str] = []
    for pair in proposal_pairs_from_manifest(manifest):
        folder_name = f"{pair.mode}__{pair.harness}"
        doc, doc_warnings = parse_portfolio_doc(run_root / "proposals" / "work" / folder_name / "proposal.toml", proposal=True)
        if doc is None:
            warnings.extend(f"{folder_name}: {warning}" for warning in doc_warnings)
            continue
        if doc.mode != pair.mode:
            warnings.append(f"{folder_name}: proposal mode field {doc.mode!r} does not match folder")
        if doc.harness != pair.harness:
            warnings.append(f"{folder_name}: proposal harness field {doc.harness!r} does not match folder")
        positions, position_warnings = normalize_positions(doc.positions, universe, label=folder_name)
        warnings.extend(position_warnings)
        sections.extend([f"## {folder_name}", "", doc.summary, "", "| ticker | weight | rationale |", "|---|---:|---|"])
        for position in positions:
            rows.append(
                {
                    "mode": pair.mode,
                    "harness": pair.harness,
                    "ticker": position.ticker,
                    "weight": f"{position.weight:.6f}",
                    "rationale": position.rationale,
                }
            )
            sections.append(f"| {position.ticker} | {position.weight:.6f} | {position.rationale} |")
        sections.append("")
    _write_csv(run_root / "proposals" / "proposals.csv", ["mode", "harness", "ticker", "weight", "rationale"], rows)
    if warnings:
        sections.extend(["## Warnings", "", *(f"- {warning}" for warning in warnings), ""])
    (run_root / "proposals" / "proposals.md").write_text("\n".join(sections), encoding="utf-8")
    for warning in warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    return selected


def first_trading_day_at_or_after(moment: datetime | None = None) -> date:
    day = (moment or datetime.now(UTC)).astimezone(UTC).date()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def finalize_portfolio(
    root: Path,
    *,
    run_id: str | None = None,
    console: Console | None = None,
    now: datetime | None = None,
) -> str:
    selected = _selected_run(root, run_id, "portfolio")
    console = console or Console()
    manifest = load_run_manifest(root, selected)
    run_root = root / "runs" / selected
    if not (run_root / "proposals" / "proposals.csv").exists():
        raise ConfigError(f"run {selected} does not have finalized proposals")
    universe = set(str(t).upper() for t in manifest.get("tickers", []))
    doc, doc_warnings = parse_portfolio_doc(run_root / "portfolio" / "work" / "portfolio.toml", proposal=False)
    if doc is None:
        raise ConfigError(f"invalid portfolio.toml for run {selected}: {'; '.join(doc_warnings)}")
    positions, warnings = normalize_positions(doc.positions, universe, label="portfolio")
    rows = [{"ticker": p.ticker, "weight": f"{p.weight:.6f}", "rationale": p.rationale} for p in positions]
    _write_csv(run_root / "portfolio" / "portfolio.csv", ["ticker", "weight", "rationale"], rows)
    trades = _trade_rows(root, positions)
    _write_csv(run_root / "portfolio" / "trades.csv", ["ticker", "current_weight", "target_weight", "delta"], trades)
    (run_root / "portfolio" / "report.md").write_text(_portfolio_report(doc, positions, trades, warnings, run_root), encoding="utf-8")
    manifest["inception"] = first_trading_day_at_or_after(now).isoformat()
    update_run_manifest(root, selected, manifest)
    for warning in warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    return selected


def _current_positions(root: Path) -> dict[str, float]:
    live = root / "live" / "portfolio.toml"
    if not live.exists() or live.stat().st_size == 0:
        return {}
    from .config import read_toml

    raw = read_toml(live)
    return {str(p["ticker"]).upper(): float(p["weight"]) for p in raw.get("positions", []) or []}


def _trade_rows(root: Path, target: list[Position]) -> list[dict[str, str]]:
    current = _current_positions(root)
    target_map = {p.ticker: p.weight for p in target}
    tickers = sorted(set(current) | set(target_map))
    return [
        {
            "ticker": ticker,
            "current_weight": f"{current.get(ticker, 0.0):.6f}",
            "target_weight": f"{target_map.get(ticker, 0.0):.6f}",
            "delta": f"{target_map.get(ticker, 0.0) - current.get(ticker, 0.0):.6f}",
        }
        for ticker in tickers
    ]


def _portfolio_report(doc: PortfolioDoc, positions: list[Position], trades: list[dict[str, str]], warnings: list[str], run_root: Path) -> str:
    turnover = 0.5 * sum(abs(float(row["delta"])) for row in trades)
    lines = [
        "# Portfolio Finalization Report",
        "",
        "## Thesis",
        "",
        doc.summary,
        "",
        "## Positions",
        "",
        "| ticker | weight | rationale |",
        "|---|---:|---|",
    ]
    for position in positions:
        lines.append(f"| {position.ticker} | {position.weight:.6f} | {position.rationale} |")
    lines.extend(["", "## Trades", "", f"Implied turnover: {turnover:.6f}", "", "| ticker | current | target | delta |", "|---|---:|---:|---:|"])
    for row in trades:
        lines.append(f"| {row['ticker']} | {row['current_weight']} | {row['target_weight']} | {row['delta']} |")
    comparison = _proposal_comparison(run_root, positions)
    if comparison:
        lines.extend(["", "## Proposal Comparison", "", comparison])
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in warnings) if warnings else lines.append("None.")
    return "\n".join(lines) + "\n"


def _proposal_comparison(run_root: Path, final_positions: list[Position]) -> str:
    proposals = run_root / "proposals" / "proposals.csv"
    if not proposals.exists():
        return ""
    final = {position.ticker: position.weight for position in final_positions}
    with proposals.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return ""
    pairs = sorted({(row["mode"], row["harness"]) for row in rows})
    tickers = sorted(set(final) | {row["ticker"] for row in rows})
    lines = ["| ticker | final | " + " | ".join(f"{m}__{h}" for m, h in pairs) + " |", "|---|---:|" + "---:|" * len(pairs)]
    for ticker in tickers:
        parts = [ticker, f"{final.get(ticker, 0.0):.6f}"]
        for mode, harness in pairs:
            value = next((float(row["weight"]) for row in rows if row["ticker"] == ticker and row["mode"] == mode and row["harness"] == harness), 0.0)
            parts.append(f"{value:.6f}")
        lines.append("| " + " | ".join(parts) + " |")
    return "\n".join(lines)
