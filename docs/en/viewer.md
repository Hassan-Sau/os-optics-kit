# Local live viewer

[os-optics-kit](../../README.md) · English · [简体中文](../zh-CN/viewer.md)

`os-optics-kit view` opens a lightweight, read-only local web page in your default browser so you can watch an optical
design change in real time while you or a coding agent edit a native Project JSON or write new saved runs.

It runs strictly on loopback (`127.0.0.1`), needs only the core `os-optics-kit` install (`os-optics>=0.13,<0.14`), and
requires **no** `[render]` or `[plot]` extras, **no** Playwright/Chromium download, and **no** cloud account.

## Quick start

```bash
python -m pip install "os-optics[protected]" os-optics-kit

# Watch a saved run directory (3D rays, power ledger, execution status and targets)
os-optics-kit view run

# Watch a native Project file (geometry preview only until a run is attached)
os-optics-kit view project.json

# Watch a Project together with its saved run (tracks freshness automatically)
os-optics-kit view project.json --run run

# Evaluate a Project once into a temporary directory without modifying the input file
os-optics-kit view project.json --evaluate
```

Useful options:

- `--port PORT` / `--host 127.0.0.1` — bind to a specific loopback port (default: OS-assigned free port; non-loopback hosts are refused).
- `--project PROJECT.json` — when opening a run directory, also track freshness against a Project file.
- `--configuration ID` — select one configuration inside a multi-configuration run directory.
- `--poll-interval SECONDS` — file-watch polling interval (default `0.4`).
- `--no-open` — start the server and print its compact JSON ready record without launching a browser.
- `--once` — print one snapshot envelope JSON to stdout and exit.

Press `Ctrl-C` to stop the server cleanly.

## What the page shows

- **Interactive 3D optical path & traced rays** — reuses the engine's local 3D viewer directly (offline, zero CDN), paired with branch color chips showing branch power and termination.
- **Power ledger** — total input power, power residual, conservation check, stacked channel proportion bar, exact channel values/units (`W`, `mW`, `µW`) and per-element breakdown.
- **Execution, integrity & freshness** — separates execution completion (`COMPLETED` vs `PARTIAL` with exhausted budgets), model scope, manifest hash integrity and freshness (`CURRENT` vs `HISTORICAL`).
- **Target assessments & observation planes** — displays `MET`, `UNMET`, `INDETERMINATE` and `NOT_APPLICABLE` assessments alongside measured values, declared bounds and bounded/untraced power intervals on partial runs.
- **Bilingual UI (`EN` / `中文`)** — switch display language directly in the top bar without restarting.

## Live-follow and safety guarantees

- **Read-only by design**: the viewer never edits your Project JSON or run directory, and refuses mutating HTTP methods (`POST`/`PUT`/`DELETE`/`PATCH` return `405`).
- **One atomic snapshot**: geometry, traced rays, power ledger, execution status and targets always come from the same verified snapshot revision. The page never mixes numbers from run A with geometry from run B.
- **Geometry preview is never a computed result**: opening a Project without a run shows nominal component/plane geometry only, with no rays and an explicit uncomputed state. `--evaluate` runs once into a temporary directory and never auto-reruns on subsequent edits.
- **No stale misattribution**: when an attached Project changes relative to its run, freshness flips to `HISTORICAL`, old numbers and rays are withheld, and the updated geometry is shown until a matching run is written.
- **Graceful recovery from partial writes**: if an agent is mid-write (truncated JSON or half-copied run), the viewer enters a waiting state while holding the last valid snapshot, then updates automatically as soon as valid files settle.

## Relation to other commands

- Use `os-optics-kit view` for **live local inspection** in your ordinary browser.
- Use `os-optics view RUN --no-browser --snapshot DIR --views iso` for browser-free static system views.
- Use `os-optics-kit render` or `os-optics-kit present` for presentation-grade figures, galleries and slide bundles.
