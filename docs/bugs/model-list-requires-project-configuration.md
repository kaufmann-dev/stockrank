# Model listing fails outside an initialized project

- Fixed: 2026-07-26 16:20:01 CEST (+0200)
- Starting commit: `92b7aa8135b68facd6dc1b3fb2fcd83d6fd23c35`

## Symptom

Running `stockrank model list` in a directory without `stockrank.toml` exited with an error instead
of reporting that no project had been configured.

## Confirmed root cause

The command passed the current working directory directly to `load_app_config`, whose first operation
required `stockrank.toml` to exist. A blank file was not a valid workaround because application
configuration also required source, default, and model tables. The global installer could not create
the file safely because it had no way to identify which current or future directories were intended
to be stockrank projects.

## Fix

- Added `stockrank init` to create a complete project-local starter configuration, mode, and
  universe from assets included in the installed package.
- Made an initialized project with zero model profiles valid and made its first added profile the
  default automatically.
- Made `stockrank model list` report both uninitialized and initialized-without-models states without
  failing.
- Added actionable initialization checks before configuration-dependent commands can prompt for or
  store credentials.
- Covered initialization, conflict safety, zero-model loading, listing, and first-profile defaulting
  with focused tests.
