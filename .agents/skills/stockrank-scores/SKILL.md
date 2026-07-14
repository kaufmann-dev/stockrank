---
name: stockrank-scores
description: Execute Stockrank score work folders by dispatching one agent per ticker and validating results.toml outputs. Use only when the user explicitly invokes this skill.
---

You are delegating the scoring of multiple stock tickers to subagents.

1. Validate the current directory contains ticker subfolders and each ticker subfolder contains `task.txt`.
2. Run one subagent per ticker folder. Use parallel batches of 10 ticker folders at a time.
3. Each subagent must follow only its ticker folder's `task.txt`, read that ticker's `data/`, and write that ticker's `results.toml`.
4. After the first pass, verify every ticker folder has a parseable `results.toml` with `ticker`, `score`, `confidence`, and `summary`.
5. Retry missing or invalid ticker results once, again by assigning only that ticker folder's `task.txt`.
6. Report the completed ticker count and list any failed tickers.

Do not score anything from these instructions. The scoring instructions live only in each rendered `task.txt`.
