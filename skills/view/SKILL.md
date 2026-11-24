---
name: view
description: Open a read-only local browser viewer for a native Project JSON or saved run directory and follow live updates.
version: 0.1.0
command: os-optics-kit view
requires: os-optics>=0.14,<0.15
kind: tool
---

# view

## When to use

- Watching a native Project JSON or a saved `os-optics` run update live in a local browser while an agent edits and re-runs the design.
- Inspecting the 3D optical path, traced branches, power ledger, execution state, observation quantities, target assessments and freshness together without modifying inputs or installing extra browser/plot packages.

## When not to use

- Exporting static system views without a browser: use `os-optics view RUN --no-browser --snapshot DIR --views iso`.
- Rendering presentation-grade figures or slide bundles: use `os-optics-kit render` or `os-optics-kit present`.
- Checking pass/fail exit codes in scripts or CI: use `os-optics-kit summary`.

## Inputs

```text
os-optics-kit view PATH [--host 127.0.0.1] [--port 0]
  [--project PROJECT.json] [--run RUN_DIR] [--configuration ID]
  [--poll-interval 0.4] [--no-open] [--evaluate] [--once]
```

`PATH` is a native Project JSON file or a saved run directory. `--host` accepts loopback addresses only (`127.0.0.1`, `localhost`, `::1`). A Project with no `--run` opens in geometry-preview mode (uncomputed, never presented as a computed result); `--evaluate` performs one local run in a temporary directory without editing `PATH` or auto-rerunning on subsequent edits.

## Outputs

Compact JSON on stdout: startup metadata with the loopback URL and read-only endpoints (`/api/snapshot`, `/api/events`, `/api/scene-document`, `/engine-view/`, `/api/health`), or one snapshot envelope with `--once`. Exit 0 on clean stop or `--once` success; exit 4 on invalid input, non-loopback host or missing engine (JSON error on stderr).

## Minimal example

```sh
os-optics-kit view run
os-optics-kit view project.json --run run
os-optics-kit view project.json --evaluate --no-open
os-optics-kit view project.json --once
```

## Limits

Strictly read-only (GET/HEAD on loopback only). Never edits inputs, never combines numbers from one run with geometry from another, withholds stale numbers when a Project changes ahead of its run, and holds the last valid snapshot with a waiting notice during partial writes.

## Version

0.1.0 (os-optics-kit 0.4.0): first version, verified with os-optics 0.13.0.
