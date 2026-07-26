# Project initialization rejects initialized projects

- Fixed: 2026-07-26 16:36:01 CEST (+0200)
- Starting commit: `7ca7e6f6e5e2a26c593f52dcac227dbbb5a35aca`

## Symptom

Running `stockrank init` in a valid stockrank project exited with an error because
`stockrank.toml`, `modes/best-bet.toml`, and `universes/liquid-50.toml` already existed.

## Confirmed root cause

Initialization treated every existing starter target as a destructive-write conflict before checking
whether `stockrank.toml` already represented a valid initialized project. Its regression test
explicitly required a repeated initialization attempt to fail, so the incorrect behavior passed the
suite.

## Fix

- Made initialization validate an existing `stockrank.toml` and return a successful no-op when it is
  valid.
- Made partial initialization validate and preserve existing starter assets while creating only
  missing files.
- Kept malformed configuration, invalid assets, and non-file targets as errors detected before any
  writes.
- Wrote the configuration last so interrupted partial initialization remains safely resumable.
- Covered fresh, repeated, customized, partial, and invalid initialization states with focused tests.
