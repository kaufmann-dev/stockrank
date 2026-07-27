# Model setup dirties project configuration

- Fixed: 2026-07-27 20:33:26 CEST (+0200)
- Starting commit: `f0263874375dfd4e941d35eed4f5fc3025772a0c`

## Symptom

Running model setup from a source checkout modified the tracked `stockrank.toml`, leaving the
checkout dirty even though the provider profile and selected model were user-specific settings.

## Confirmed root cause

Model profiles and `defaults.model` were stored in the current project's `stockrank.toml`.
`stockrank init` and `stockrank model add` therefore wrote directly into whichever initialized
project was the current directory.

## Fix

- Moved named model profiles and the selected default model to the XDG user configuration file:
  `$XDG_CONFIG_HOME/stockrank/config.toml`, with `~/.config/stockrank/config.toml` as the fallback.
- Removed model configuration from project starter and repository configuration files.
- Made project configuration reject model settings, so profiles have one authoritative global home.
- Added focused coverage that a model addition does not modify project configuration.
