# execute-proposals

Use this skill from a `runs/<id>/proposals/work/<mode>__<harness>/` folder.

1. Validate the current directory contains `task.txt`, `scores.csv`, `rationales.csv`, and `report.md`.
2. Parse the expected mode and harness from the current folder name, splitting on `__`.
3. Ask the user to confirm that this session is running in the expected harness. Abort if they do not confirm.
4. Follow `task.txt` exactly and write `proposal.toml`.
5. Verify that `proposal.toml` is parseable TOML with `mode`, `harness`, `summary`, and `positions`.
6. Verify that `mode` and `harness` match the folder name.
7. Verify the position weights sum to approximately 1.0.
8. Report whether the proposal is complete and name any validation failures.

Do not construct a portfolio from these instructions. The construction instructions live only in `task.txt`.
