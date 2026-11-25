---
name: zoom
description: Zoom study of a fixed prescription: solve two moving groups' positions per requested focal length with the focus held on a fixed image plane (engine paraxial analysis), write them as named configurations of one Project, report achieved EFL, focus error, travel and clearance per sample.
version: 0.1.0
command: os-optics-kit zoom
requires: os-optics>=0.14,<0.15
kind: tool
---

# zoom

## When to use

- A coaxial design has a fixed group, a variator and a compensator, and you need their positions over a focal-length
  range with the image on one plane: a cam table, a check of known zoom positions, or the range limits that travel
  and mount clearance allow.

## When not to use

- One parameter grid without solving: `sweep`. Optimizing the prescription itself: not this tool.
- Folded paths (mirrors), more or fewer than two moving groups: refused.

## Inputs

`os-optics-kit zoom SPEC.json --out DIR [--base P.json] [--overwrite] [--study [--workers N]]`. Spec keys (mm):

- `base_project` (relative to the spec), `wavelength_nm`;
- `groups`: every element in light order, `{group_id, components, moving, travel_mm [min, max], envelope_mm
  [before, after]}`; exactly two move;
- `image_plane`: `{observation_id}` or `{position_mm}`;
- `efl_mm`: `{start, stop, step}` or `{values}`;
- `anchors`: known solutions, `{efl_mm, configuration_id}` (positions from that configuration of the base) or
  `{efl_mm, positions_mm: {group: mm}}`; a `configuration_id` also names the configuration written;
- `tolerances`: `{efl_mm, focus_mm}`; `clearance`: `{min_mm}`; optional `solver`.

## Outputs

`DIR/project.json` (one configuration per solved sample: anchors keep their id, others `efl_042p50`),
`trajectory.json`, `trajectory.csv` and a Markdown report of the same table; with `--study`, `DIR/study` (a sweep
study, parameter `efl_mm`, for `compare`, `plot sweep`, `report`, `present`). Per sample: requested and achieved EFL, positions, focus error
(rear focal point minus image plane along the axis), clearances, `status` (`PASS`, `FAIL`, `INFEASIBLE`,
`UNSOLVED`) and `reasons` (`TRAVEL_EXCEEDED`, `CLEARANCE_VIOLATED`, `EFL_OUT_OF_TOLERANCE`,
`FOCUS_OUT_OF_TOLERANCE`, `BRANCH_MISMATCH`, solver codes). Stdout: compact JSON with counts, coverage
(`covered_fraction`, `pass_intervals_mm`), worst values, anchors and the samples that are not PASS, all over the
requested samples; an anchor outside the requested range seeds the solve and is reported apart under
`anchors_outside_range`. Exit 0 all PASS,
1 otherwise, 4 refused (`SPEC_INVALID`, `UNKNOWN_ID`, `OUTPUT_EXISTS`, `AXIS_FOLDED`, `IMAGE_PLANE_OFF_AXIS`).

## Minimal example

```sh
os-optics-kit cases copy three-group-zoom --dest .
cd three-group-zoom
os-optics-kit zoom studies/zoom.json --out zoom --study
os-optics-kit view zoom/project.json --configuration tele
os-optics-kit zoom studies/zoom_constrained.json --out constrained
```

## Limits

- Paraxial: object at infinity, image of the rear focal point; aberrations are not part of the solve (run the
  configurations, `--study`, for traced beams).
- Travel and clearance are checked, never imposed: violations are reported with the solved position.
- One solution branch, followed from the anchors; a sample past a fold is UNSOLVED.
- Clearance is axial, from vertices plus declared envelopes.

## Version

0.1.0: first version, for os-optics 0.14.
