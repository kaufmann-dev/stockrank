---
name: stockrank-proposals
description: Execute Stockrank proposal work folders by following task.txt and validating proposal.toml outputs. Use only when the user explicitly invokes this skill.
---

You are constructing a portfolio proposal based on the scores and rationales of multiple stock tickers.

1. Validate the current directory contains `task.txt`, `scores.csv`, `rationales.csv`, and `report.md`.
2. Parse the expected mode and harness from the current folder name, splitting on `__`.
3. Follow `task.txt` exactly and write `proposal.toml`.
4. Verify that `proposal.toml` is parseable TOML with `mode`, `harness`, `summary`, and `positions`.
5. Verify that `mode` and `harness` match the folder name.
6. Verify the position weights sum to approximately 1.0.
7. Report whether the proposal is complete and name any validation failures.

Do not construct a portfolio from these instructions. The construction instructions live only in `task.txt`.
