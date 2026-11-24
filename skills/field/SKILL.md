---
name: field
description: Field curvature and distortion of a saved os-optics run per field point and wavelength, from the engine's published ray data and the run's source poses: image height, distortion against EFL tan(theta), tangential and sagittal focus, astigmatism.
version: 0.1.0
command: os-optics-kit field
requires: os-optics>=0.14,<0.15
kind: tool
---

# field

## When to use

- A run with several field points exists and you need distortion and field curvature at an image plane.

## When not to use

- For ray-fan curves of one field: use `fans`.
- For MTF or PSF: use `os-optics results imaging`.

## Inputs

- `RUN`: a saved run (with `project.json` and `--ray-data full`). Never modified; nothing is traced.
- `--monitor PLANE`: the image plane.
- `--efl MM`: the effective focal length for the distortion reference. Without it the smallest non-zero field is
  the reference (it reads 0 by construction; the output names it).
- `--out FILE`: write the JSON there.

## Outputs

One JSON document: `references` per wavelength (`efl_mm`, `basis`) and `fields` rows (`path_id`, `wavelength_nm`,
`field_angle_deg`, `image_height_mm`, `reference_height_mm`, `distortion_pct`, `tangential_focus_mm`,
`sagittal_focus_mm`, `astigmatism_mm`). Exit 0, or 4 with a JSON error (`SOURCE_POSE_MISSING`,
`PROJECT_UNREADABLE`, and the `fans` errors).

## Conventions

- Field angle: between a source's emission axis and that of the on-axis source (the chief ray nearest the plane
  origin).
- Distortion: (h - EFL tan(theta)) / (EFL tan(theta)), in percent.
- Focus: along the plane normal from the plane, positive behind it, from the innermost pupil zone of each fan.

## Minimal example

```
os-optics project run project.json --out-dir run
os-optics-kit field run --monitor image_plane --efl 50
```

## Limits

- Geometric optics; one plane crossing. Focus comes from the innermost sampled zone, not an exact paraxial trace.
- Assumes no image rotation in the path and a rotationally symmetric reference (h = EFL tan(theta)).

## Version

0.1.0 (os-optics-kit 0.5.0), for os-optics 0.14.
