# field: aperture stop and reflected paths

Read this page when a row's `chief_ray` is not `WITHIN_STOP_TOLERANCE`, when the run has a decentred or sampled
stop, or when the command refuses with `REFLECTED_PATH_UNSUPPORTED`.

## Stop crossing

- `aperture_stops`: every stop of the run: `stop_id` (the Engine surface id, `STOP/stop`; a unit member's
  `UNIT/MEMBER-stop`), `opening_center_mm` (the stop's `opening_center_mm`, local X, Y in its plane), `centre_mm`
  (that point in the design frame), `normal`, and `geometry_issue` when the scene does not give them.
- The crossing is the chief-ray segment that ends on a stop's `stop_id`, whatever its event: a stop holding a sample
  ends it with `complex_transmission`, not `aperture_stop` (`identified_by: ENGINE_SURFACE_ID`). A segment without
  a surface id counts only when its `aperture_stop` end lies in the plane of exactly one stop (`STOP_PLANE`).
- `stop_offset_mm`: distance from the opening centre in the stop plane (the largest when several stops are crossed).
  A stop with `opening_center_mm` [1.5, 0] and a source on its axis reads 1.5 mm, not 0.

`chief_ray.status`:

| status | meaning | distortion, focus |
|---|---|---|
| `WITHIN_STOP_TOLERANCE` | offset at most `stop_tolerance_mm` | reported |
| `CHIEF_RAY_OFF_STOP` | offset larger: aim the source at the opening centre | null |
| `CHIEF_RAY_STOP_NOT_CROSSED` | the run has a stop and the ray does not cross it | null |
| `CHIEF_RAY_STOP_UNRESOLVED` | crossing or stop geometry not resolved; `detail` says why | null |
| `NO_APERTURE_STOP` | no stop: the beam-centre ray as given | reported |

`CHIEF_RAY_STOP_UNRESOLVED` covers an `aperture_stop` event on a surface that is not a stop of the scene, an end
point off the stop plane, an opening centre that is not two numbers, and a crossing without a surface id that
matches no single stop plane.

## Reflected paths

Field rows need the pupil orientation at the plane, which is not resolved for a path reflected before it (mirror,
total internal reflection). `--efl` does not change this.

- Only reflected paths reach the plane: exit 4 with

```
{"error": {"code": "REFLECTED_PATH_UNSUPPORTED", "message": "every path reaching near is reflected before it ...",
           "details": {"monitor": "near", "reflected_paths": [{"path_id": "SRC#0", "reflections": 1}]}}}
```

- A reflected path beside unreflected ones: listed in `unreached_fields` with `PUPIL_FRAME_UNRESOLVED`; the others
  have rows.
