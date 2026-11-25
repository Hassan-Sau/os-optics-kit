# tolerance: compensation

Read this page when a tolerance spec declares `compensation`: for every sample, the compensator offsets that
minimise the objective are solved and the compensated Project is run.

## Spec

```
"compensation": {
  "objective": [{"target": "spot_radius", "goal": 0, "scale": 0.02}],
  "compensators": [{"name": "refocus", "unit": "mm", "nominal": 0, "bounds": [-2, 2], "tolerance": 0.002,
                    "ops": [{"op": "transform", "selector": {"ids": ["focus"]}, "frame": "design",
                             "translation_mm": ["$value", 0, 0]}]}],
  "max_evaluations": 100, "max_sweeps": 4, "cost_tolerance": 1e-6
}
```

- Objective: `sum(((value - goal) / scale)^2)` over exact target values only; goal and scale in the target unit.
- `bounds`: offsets from the as-built state, low < 0 < high. `"$value"` = nominal + offset, as for parameters.
- Solver: coordinate descent with golden-section line searches within the bounds, starting at offset 0. A trial
  whose objective value is not exact is never selected.
- `"enabled": false` or `--no-compensation`: the same samples, as-built only.

## Checks before any run

- `SPEC_INVALID`: the compensator ops fail at offset 0 or at a bound.
- `NOMINAL_MISMATCH`: at offset 0 the ops change the base Project; state the base value or omit `nominal`.
- `COMPENSATOR_OVERLAP`: at offset 0 a compensator changes an as-built sample, for example a set_* op on a field a
  parameter perturbs. Use a transform op, which moves the as-built component further.

## Results

- Stages: `nominal` (s0000), `as_built` (the sample record), `compensated` (`compensated:*` columns).
- `counts`, `target_distributions` and the exit code are the as-built outcomes. The `compensation` block (stdout
  and `summary.json`) holds the stage counts, `compensated_or_as_built`, `states` and `failure_reasons`.
- `states`: `SOLVED`; `FAILED` with a reason (`AS_BUILT_ERROR`, `EVALUATION_ERROR`, `NOT_CONVERGED`, `BOUNDS_HIT`,
  `RUN_STOPPED`, `WORSE_THAN_AS_BUILT`), keeping the as-built result; `NOT_EVALUATED` (`COMPENSATION_NOT_RUN`): the
  sample ran without the compensation evaluator. `solved_without_improvement` counts SOLVED samples no better than
  as-built.

## Running

- The study records its evaluator, `os_optics_kit.study.compensation:run_compensated`, in `study.json`.
  `study run DIR` (with or without `--resume`) and `tolerance SPEC --resume` use it, and every printed `next`
  command names it; `study run --evaluator` with another one is refused before anything is written
  (`EVALUATOR_MISMATCH`, with `recorded` and `given`).
- Runs: at most `runs_max = 1 + samples * (compensation.max_evaluations + 2)`. The top-level `max_evaluations`
  caps nominal + samples only. `--time-limit-s` and `--max-memory-mib` apply to each Engine run.

Examples: `cases/expansion-focus/studies/tolerance_expander_refocus.json` (refocus),
`cases/mirror-alignment/studies/tolerance_alignment_compensated.json` (mirror alignment).
