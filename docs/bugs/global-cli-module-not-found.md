# Global CLI failed to import stockrank

- Fixed: 2026-07-26 13:23:26 CEST (+0200)
- Starting commit: `5bef338e518825e82c907088c1b2c1fbba7a991f`

## Symptom

Running the global `stockrank` command failed with
`ModuleNotFoundError: No module named 'stockrank'`.

## Confirmed root cause

`~/.local/bin/stockrank` was an orphaned legacy launcher from July 3 that hardcoded
`/usr/bin/python3`. The `stockrank` package was not installed for that interpreter, and `uv tool
list` confirmed that the launcher was not managed by `uv`. The project-local
`.venv/bin/stockrank` launcher worked.

## Fix

- Replaced the orphaned launcher with an editable `uv` tool installation linked to this checkout.
- Updated the README installation flow to install the global command explicitly.
- Verified that the global launcher uses the isolated `uv` tool interpreter and that both
  `stockrank version` and `stockrank --help` succeed.
