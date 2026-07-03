# execute-portfolio

Use this skill from `runs/<id>/portfolio/work/`.

1. Validate the current directory contains `task.txt`, upstream score/proposal files, `current.csv`, and `current.md`.
2. Parse the expected harness from `run.toml` if accessible through the run root, then ask the user to confirm that this session is running in that harness. Abort if they do not confirm.
3. Follow `task.txt` exactly and write `portfolio.toml`.
4. Verify that `portfolio.toml` is parseable TOML with `summary` and `positions`.
5. Verify the position weights sum to approximately 1.0.
6. Report whether the portfolio is complete and name any validation failures.

Do not allocate from these instructions. The allocation instructions live only in `task.txt`.
