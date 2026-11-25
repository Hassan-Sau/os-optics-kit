---
name: three-group-zoom
title: Three-group zoom with a fixed image plane
summary: Ideal thin lenses f = 100, -30 and 50 mm form a 35-105 mm zoom; os-optics-kit zoom solves the variator and compensator positions per focal length with the focus held on a fixed image plane, checked against an independent thin-lens calculation.
class: lens
domain: imaging
provenance: self-built
publication: own-work
engine: 0.14.0
catalog: none
project: project.json
tools: zoom, sweep, compare, plot, report, view
tags: ideal-lens, parameter-sweep, requirement-check, independent-reference
license: MIT
---

## What it shows

G1 (f = 100 mm) is fixed at x = 0, the variator G2 (f = -30 mm) and the compensator G3 (f = 50 mm) move, the
image plane stays at x = 200 mm, 633 nm. `project.json` is the 70 mm position. `os-optics-kit zoom` solves both
positions per focal length from the engine's paraxial analysis, writes a Project with one named configuration per
sample and reads the achieved EFL, focus error and clearances back from it. The compensator reverses near 53.7 mm.
`studies/zoom_constrained.json` narrows the travel and widens the range: the samples outside it are reported
INFEASIBLE with their unclipped positions.

## Run

```bash
os-optics project run project.json --out-dir run
os-optics-kit zoom studies/zoom.json --out zoom --study
os-optics-kit view zoom/project.json --configuration tele
os-optics-kit plot sweep zoom/study --y target:axis_spot
os-optics-kit zoom studies/zoom_constrained.json --out constrained
python figures.py zoom --out images
```

The constrained study exits 1: 15 PASS, 6 INFEASIBLE.

## Targets

| Target | Metric | Plane | Bound | Verified (70 mm) |
| --- | --- | --- | --- | --- |
| `axis_power` | `power_W` | `image` | ≥ 0.00099 W | MET |
| `axis_spot` | `beam_radius_mm` | `image` | ≤ 0.025 mm | 0.01410 mm, MET |

Zoom study (29 samples, 35-105 mm, step 2.5 mm): every sample PASS, largest |EFL error| 2.0e-8 mm, largest
|focus error| 1.8e-9 mm, least clearance 20.632 mm (front/variator at 35 mm).

## Baseline

os-optics 0.14.0, no catalog: run status PASS (`expected.json`).
`os-optics-kit cases verify three-group-zoom` repeats the run and compares it.

## Files

- `project.json`, `expected.json`: the 70 mm design and its outcome.
- `studies/zoom.json`, `studies/zoom_constrained.json`: zoom specs.
- `figures.py`: draws the trajectory from a zoom study.
- `README.md`, `README.zh-CN.md`: the illustrated pages; `images.json` records `images/`.
