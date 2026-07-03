from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from stockrank.cli import choose_strategy
from stockrank.config import ConfigError, load_run_manifest, resolve_strategy
from stockrank.prepare import prepare_scores

from conftest import FakeMassiveClient, ROOT


def test_seed_universes_are_real_non_empty_ticker_lists() -> None:
    for path in (ROOT / "universes").glob("*.toml"):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        tickers = data["tickers"]
        assert tickers
        assert all(isinstance(ticker, str) and ticker and "..." not in ticker for ticker in tickers)
        assert all(ticker not in {"TBD", "TODO", "FAKE"} for ticker in tickers)
    global_titans = tomllib.loads((ROOT / "universes" / "global-titans-50.toml").read_text(encoding="utf-8"))
    assert len(global_titans["tickers"]) == 50


def test_strategy_resolution_validates_phase_and_duplicate_pairs(mini_project: Path) -> None:
    resolved = resolve_strategy(mini_project, "mini")
    assert resolved.planned_score_evaluations == 4
    (mini_project / "modes" / "builder.toml").write_text(
        'name = "builder"\nphase = "scores"\npersona = "x"\ntask = "x"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="declares phase"):
        resolve_strategy(mini_project, "mini")
    shutil.copy2(ROOT / "modes" / "balanced-builder.toml", mini_project / "modes" / "builder.toml")
    (mini_project / "modes" / "builder.toml").write_text(
        'name = "builder"\nphase = "proposals"\npersona = "x"\ntask = "x"\n',
        encoding="utf-8",
    )
    (mini_project / "strategies" / "mini.toml").write_text(
        (mini_project / "strategies" / "mini.toml").read_text(encoding="utf-8").replace(
            'runs = [\n  { mode = "builder", harness = "codex" },\n]',
            'runs = [\n  { mode = "builder", harness = "codex" },\n  { mode = "builder", harness = "codex" },\n]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate proposal pair"):
        resolve_strategy(mini_project, "mini")


def test_interactive_strategy_picker_uses_default_number(mini_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stockrank.cli.Prompt.ask", lambda *args, **kwargs: kwargs["default"])
    assert choose_strategy(mini_project) == "mini"


def test_prepare_scores_writes_snapshot_and_ticker_workdirs(mini_project: Path) -> None:
    run_id = prepare_scores(mini_project, "mini", client=FakeMassiveClient())
    manifest = load_run_manifest(mini_project, run_id)
    assert manifest["strategy"]["name"] == "mini"
    assert manifest["tickers"] == ["AAA", "BBB"]
    task = mini_project / "runs" / run_id / "scores" / "work" / "quality" / "codex" / "AAA" / "task.txt"
    text = task.read_text(encoding="utf-8")
    assert "Your working folder is `AAA`" in text
    assert "`AAA/data/`" in text
    assert (task.parent / "data" / "details.json").exists()
