---
name: stockrank-portfolio
description: Execute Stockrank final portfolio work folders by following task.txt and validating portfolio.toml outputs. Use only when the user explicitly invokes this skill.
---

You are constructing a final portfolio based on portfolio proposals and upstream scores and rationales of multiple stock tickers.

1. Validate the current directory contains `task.txt`, upstream score/proposal files, `current.csv`, and `current.md`.
2. Follow `task.txt` exactly and write `portfolio.toml`.
3. Verify that `portfolio.toml` is parseable TOML with `summary` and `positions`.
4. Verify the position weights sum to approximately 1.0.
5. Report whether the portfolio is complete and name any validation failures.

Do not allocate from these instructions. The allocation instructions live only in `task.txt`.
