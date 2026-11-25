# Zoom studies

[os-optics-kit](../../README.md) · English · [简体中文](../zh-CN/zoom.md)

A zoom lens keeps one prescription and one image plane and moves groups to change its focal length. For a fixed
group, a variator and a compensator, two conditions fix the two moving positions at each focal length: the system's
effective focal length (EFL) equals the requested one, and the paraxial image lies on the image plane.
`os-optics-kit zoom` solves these conditions over a focal-length range with the engine's paraxial analysis, writes
the solutions as named configurations of one native Project, and reports every sample with its achieved values,
its travel and clearance checks and a status.

The worked example is the case [three-group-zoom](../../cases/three-group-zoom): ideal thin lenses f = 100, −30 and
50 mm, image plane at x = 200 mm, 35-105 mm.

```bash
os-optics-kit cases copy three-group-zoom --dest .
cd three-group-zoom
os-optics-kit zoom studies/zoom.json --out zoom --study
```

## The spec

```json
{
  "schema": "os-optics-kit.zoom-spec", "schema_version": 1,
  "base_project": "../project.json",
  "wavelength_nm": 633,
  "groups": [
    {"group_id": "front", "components": ["G1"], "envelope_mm": [0, 3]},
    {"group_id": "variator", "components": ["G2"], "moving": true, "travel_mm": [10, 70], "envelope_mm": [3, 3]},
    {"group_id": "compensator", "components": ["G3"], "moving": true, "travel_mm": [100, 150], "envelope_mm": [3, 3]}
  ],
  "image_plane": {"observation_id": "image"},
  "efl_mm": {"start": 35, "stop": 105, "step": 2.5},
  "anchors": [
    {"efl_mm": 35, "configuration_id": "wide", "positions_mm": {"variator": 26.6323, "compensator": 124.7022}},
    {"efl_mm": 70, "configuration_id": "mid", "positions_mm": {"variator": 47.9331, "compensator": 124.2553}},
    {"efl_mm": 105, "configuration_id": "tele", "positions_mm": {"variator": 59.9205, "compensator": 132.3609}}
  ],
  "tolerances": {"efl_mm": 0.01, "focus_mm": 0.005},
  "clearance": {"min_mm": 1.0}
}
```

| Key | Meaning |
| --- | --- |
| `groups` | Every element of the path in the order light meets them. A group moves rigidly along the optical axis; exactly two groups move. `travel_mm` is a moving group's allowed range, `envelope_mm` the axial extent of its mount before its first and after its last vertex. |
| `image_plane` | An observation plane of the Project (fixed in the design frame) or a point. It must lie on the optical axis. |
| `efl_mm` | The requested focal lengths: `{start, stop, step}` (stop included) or `{values}`. |
| `anchors` | Known solutions: the positions of the moving groups at a focal length, given directly or read from a configuration of the base Project (`{efl_mm, configuration_id}`). They seed the solver and check its branch, and may lie outside the requested range. |
| `tolerances` | Acceptance of each sample: \|EFL error\| and \|focus error\| in mm. |
| `clearance` | The least axial gap between envelopes, and between the last envelope and the image plane. |
| `solver` | Optional: `tolerance_mm` (1e-7), `max_iterations` (30), `max_refinements` (8), `branch_jump_mm` (1.0), `anchor_tolerance_mm` (0.01), `derivative_step_mm` (1e-4). |

A group's position is the design-frame coordinate along the optical axis of its first component's pose origin
(x for an axis along +x). Lengths are in mm.

## How the positions are solved

Each sample is a Newton solve of the two conditions in the two positions. The model is the engine's paraxial
analysis (`os_optics.local.json_api.paraxial_analysis`, the same analysis as `os-optics project paraxial`) of the
base Project with the moving groups placed; the Jacobian is taken by central differences. The solver makes no
thin-lens assumption: it uses what the engine's paraxial analysis computes for the listed components (the example
uses ideal thin lenses).

The focal lengths are visited by continuation:

1. each anchor is solved from its declared positions;
2. from the first anchor the solver steps down to the start of the range; from each anchor it steps up to the next
   anchor; from the last anchor it steps up to the end;
3. each step starts from a predictor: the tangent of the solution curve at the anchor, then the secant through the
   last two solutions;
4. a step whose solve does not converge, or whose solution lies more than `branch_jump_mm` from the predictor
   (another branch of the zoom equation), is retried through a half step, up to `max_refinements` halvings;
5. where a march reaches the next anchor's focal length, its solution must lie within `anchor_tolerance_mm` of
   that anchor's own solution, otherwise the anchor is reported `BRANCH_MISMATCH`.

The compensator may reverse direction along the way; in the example it does near 53.7 mm. Travel and clearance are
not imposed on the solve: they are checked afterwards, so a position outside its travel is reported as it was
solved.

## What is reported, and on which basis

Every solved sample becomes a configuration of `DIR/project.json`; an anchor keeps its `configuration_id`, the
others are named after their focal length (`efl_042p50`). The base Project's targets that name configurations
other than the anchors' are dropped and listed under `dropped_targets`. The reported values are then read from the
engine's paraxial analysis of `DIR/project.json`, one configuration at a time:

| Value | Basis |
| --- | --- |
| EFL achieved, EFL error | the system EFL, −1/C of the reduced ABCD matrix, minus the requested EFL |
| Focus error | the rear focal point (design frame) along the axis minus the image plane along the axis: the paraxial image of an object at infinity, positive behind the plane |
| Positions | along-axis coordinate of each group's first component in that configuration |
| Clearances | entrance and exit vertices of the elements, extended by the envelopes; the least one and the pair it lies between |

| Status | Meaning |
| --- | --- |
| `PASS` | both tolerances met, travel and clearance hold |
| `INFEASIBLE` | solved, but a group leaves its travel (`TRAVEL_EXCEEDED`) or a gap is below the minimum (`CLEARANCE_VIOLATED`) |
| `FAIL` | solved, but a tolerance is missed (`EFL_OUT_OF_TOLERANCE`, `FOCUS_OUT_OF_TOLERANCE`) or the branch check failed (`BRANCH_MISMATCH`) |
| `UNSOLVED` | no solution: `NOT_CONVERGED`, `SINGULAR_JACOBIAN` (a fold), `BRANCH_JUMP`, `AFOCAL`, `ENGINE_REFUSED`, or `NO_SEED` when no anchor converged |

The requested range runs from the smallest to the largest requested focal length. An anchor inside it is a
sample like the others. An anchor outside it (a 105 mm anchor with a 35-70 mm request) still seeds the continuation
and takes part in the branch check, and is solved, written as a configuration and evaluated, but it is reported
apart: under `anchors_outside_range` in `trajectory.json` (sample ids `a001`, ...), in its own section of
`report.md`, and with `in_requested_range: false` on its anchor record. It never enters the counts, the coverage,
the step statistics, the worst values, `trajectory.csv`, the `--study` samples or the status.

No sample is dropped. `trajectory.json` holds every sample with its reasons (each with the values it concerns),
the anchors with their declared and solved positions, branch checks, sample id and status, the anchors outside the
requested range, the coverage of the requested range (`covered_fraction`: the share of the requested range lying
between two adjacent PASS samples of that range, `pass_intervals_mm`, the runs of samples that are not PASS, the
largest and smallest step), the worst EFL error, focus error and clearance, the solver settings and the number of
engine evaluations, and the engine version and build. `trajectory.csv` is the table of the requested range and
`report.md` presents it. The command exits 0 when every sample of the requested range passes and 1 otherwise.

## Look at the result

```bash
os-optics-kit view zoom/project.json --configuration efl_087p50      # geometry of one configuration
os-optics project run zoom/project.json --out-dir zoom/run          # every configuration, traced
os-optics-kit view zoom/run --configuration tele
os-optics results configurations zoom/run                            # the configurations side by side
```

With `--study` the command also runs every configuration through the sweep machinery (parameter `efl_mm`; an
unsolved sample is a refused sample), so the study tools apply: `--workers`, `--resume`,
`os-optics-kit plot sweep zoom/study --y target:axis_spot`, `os-optics-kit compare zoom/study`,
`os-optics-kit report zoom/study --project zoom/project.json`, and `present init RUN zoom/study --animate
--set assets.playback-study.y=target:axis_spot` for a playback of the samples that plots the spot radius. In the example the on-axis spot radius on the image plane grows as λf / (πw₀) from
0.00705 mm at 35 mm to 0.02115 mm at 105 mm: the traced beam stays focused on the fixed plane.

## Limits

- The solve is paraxial; aberrations of the solved configurations show in their traced runs.
- Two moving groups, one straight axis (no mirrors), an object at infinity.
- One solution branch, followed from the anchors. Past a fold of the zoom equation the samples are `UNSOLVED`.
- Clearance is axial: vertex positions plus the declared envelopes.
