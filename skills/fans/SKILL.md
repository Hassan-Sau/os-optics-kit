---
name: fans
description: Ray fans of a saved os-optics run: transverse and longitudinal aberration and per-ray OPD across the pupil, per field point and wavelength, from the engine's published ray data, with the engine's spot and OPD numbers beside them.
version: 0.1.0
command: os-optics-kit fans
requires: os-optics>=0.14,<0.15
kind: tool
---

# fans

## When to use

- A run directory exists and you need ray-fan curves at an image plane: transverse aberration versus pupil
  coordinate, longitudinal aberration, or OPD, per field and wavelength.
- You want those curves checked against the engine's own geometric spot radius and wavefront RMS.

## When not to use

- For PSF, MTF or encircled energy: use `os-optics results imaging`.
- For the wavefront of one path only: use `os-optics results wavefront`.
- To change the design: use `edit`.

## Inputs

- `RUN`: a saved run directory written with `--ray-data full` (the default). Never modified; nothing is traced.
- `--monitor PLANE`: the observation plane (an image plane).
- `--path ID` (repeatable): limit to these paths; default every path at the plane.
- `--zones N`: keep N of the engine's pupil rings.
- `--imaging FILE`: a `results imaging` document; its `geometric_spot_rms_radius` is recorded per path.
- `--out FILE`: write the JSON there.

## Outputs

One JSON document: per path (`path_id`, `source_id`, `wavelength_nm`, `chief_uv_mm`), `fans.tangential`
and `fans.sagittal` rows (`rho`, `zone`, `transverse_um`, `longitudinal_mm`, `opd_waves`), `spot_rms_um`,
`opd_rms_waves`, and `engine` (`spot_rms_um`, `opd_rms_waves`, `opd_pv_waves`). Exit 0, or 4 with a JSON
error on stderr (`CHIEF_RAY_MISSING`, `PUPIL_RAYS_MISSING`, `PATH_NOT_FOUND`, `ENGINE_RESULTS_FAILED`).

## Conventions

- Pupil coordinate: launch offset normalized by the outermost ring (rho = 1 at the edge). Tangential fan:
  pupil y axis; sagittal: pupil x axis.
- Transverse aberration: image-plane (u, v) minus the chief ray's, in micrometres.
- Longitudinal aberration: distance along the chief ray, from the plane, where a fan ray crosses it; null for
  a ray parallel to the chief ray.
- OPD: the engine's per-ray OPD (reference plane through the chief ray, piston removed), in waves.

## Minimal example

```
os-optics project run project.json --out-dir run
os-optics results imaging run --monitor image_plane --request imaging-request.json > imaging.json
os-optics-kit fans run --monitor image_plane --imaging imaging.json --out fans.json
```

## Limits

- Geometric optics only; no diffraction.
- `spot_rms_um` uses the run's saved pupil rays; the engine's imaging spot re-traces a denser grid, so small
  spots differ by the sampling (compare both; the engine's value is the reference).
- Crossing index 0 only (the first arrival at the plane).

## Version

0.1.0 (os-optics-kit 0.5.0), for os-optics 0.14.
