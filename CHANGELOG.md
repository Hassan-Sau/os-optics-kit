# Changelog

Releases of os-optics-kit, newest first. Each release states the os-optics range it requires and the os-optics
version and catalog release it was verified with (`baseline.json`). A release heading is the version on PyPI and
the tag `vX.Y.Z`; `python scripts/check_repo.py` requires a section for the version in `baseline.json`.

## 0.4.0

Requires `os-optics>=0.13,<0.14`; verified with os-optics 0.13.0 and catalog release 2026-10-28.1.

### Added

- Study execution (`os_optics_kit.study`): `sweep`, `sensitivity`, `tolerance`, `study candidates` and the design
  workflows of the cases plan their samples as requests, execute them through one executor and aggregate the saved
  records. A study run with several workers equals the serial run: sample ids, values and result hashes come from
  the plan, not from scheduling.
- `--workers N|auto`, `--resume`, `--plan-only`, `--time-limit-s` and `--max-memory-mib` on `sweep`,
  `sensitivity`, `tolerance` and `study candidates`; `--workers N|auto` on the design workflows of the cases.
- `os-optics-kit study run | status | aggregate | candidates | bench`: execute a saved plan, inspect a study,
  rebuild its tables without running the Engine, compare a set of Project files, and measure how worker processes
  scale on your machine.
- Study directory: `study.json` (plan hash, Engine version and build, catalog release, kit version), `plan.jsonl`,
  `requests/`, `journal.jsonl`, `samples/<id>/sample.json`. `--resume` reuses a sample only when its request, the
  Engine build and the catalog release are unchanged.
- `sweep` takes any number of parameters. `plot sweep --fix NAME=VALUE` (asset key `fix` in `present`) plots a
  slice; a selection that leaves more than two parameters free is refused with `PLOT_NEEDS_SLICE`, which names
  them and their value counts.
- Value kinds of target values: study tables add `target:<id>:value_kind` (`EXACT`, `BOUNDED`,
  `QUANTITY_UNKNOWN`, `OUTSIDE_MODEL`, `UNDECIDED`, `NO_VALUE`). `value_use: "reported"` in sweep, sensitivity and
  tolerance specs and `compare --value-use reported` use every finite value and state the kinds used; the default
  `exact` uses `EXACT` values only, as before.
- Study rows `execution_outcome` and `budgets_exhausted`, counts `by_execution`, and the sweep constraint
  `{"execution": ["COMPLETED"]}`, which makes only runs that reached no compute budget feasible.
- `sensitivity` and `tolerance`: an omitted `nominal` of a parameter with `set_*` ops is read from the base Project
  (`nominal_source` in `study.json` and `summary.json`); ops whose fields hold different base values refuse the
  parameter (`NOMINAL_AMBIGUOUS`).
- `sensitivity` accepts one step size per parameter: 1 + 2N runs, no step-consistency check (flag
  `STEP_CHECK_UNAVAILABLE`).
- `--overwrite` on `cases verify --out` (replaces the case's run only) and `measurements --out` (replaces the five
  files it writes; other files stay).
- Skill `study`; guide "Studies: plan, execute, aggregate, present" (`docs/en/studies.md`, `docs/zh-CN/studies.md`).
- Cases `achromatic-imaging`, `aspheric-lens`, `bounded-aberration-correction`, `cylindrical-light-sheet`,
  `focus-requirement`, `midwave-infrared-imager`, `phase-only-flat-top`, `sonnar-telephoto-lens` (18 in total).

### Changed

- The requirement class of a run (`summary`, `edit --run`, study outcomes) is the class of its selected targets'
  assessments. A `PARTIAL` run whose targets are all met is `PASS` (exit 0; it was `UNKNOWN`, exit 2, since 0.2.0),
  a `PARTIAL` run without targets is `NOT_APPLICABLE`, and a target a reached budget left undecided keeps the run
  `UNKNOWN`. An unmet target makes the run `FAIL` also when another target is outside its model.
  `outcome.execution` and the budgets stay beside the class; a `FAILED` or `CANCELLED` run is still an error
  (exit 4).
- `sweep`, `sensitivity` and `tolerance` write a study directory with the files above next to the tables and
  reports they wrote before. The tables list the samples in plan order.
- `max_evaluations` is optional in sweep, sensitivity and tolerance specs (absent: no cap); `--max-evaluations` of
  the design workflows defaults to no bound. A stated cap is checked before any run, as before.
- A study value whose Project cannot be built is one refused sample (stage `build`); the other values run.
- A study interrupted with Ctrl-C exits 130 and prints the command that resumes it. A refused design, a time or
  memory limit or a lost worker fails its own sample; the other samples complete.
- A study directory is owned by one execution; a second run, aggregation or re-plan is refused (`STUDY_RUNNING`),
  and a file system without file locks is refused (`STUDY_LOCK_UNAVAILABLE`).
- `summary`, `compare` and `report` read a run whose files do not match their recorded hashes as stored, with
  `integrity.consistent: false` and the problems (`summary` also writes a `RUN_INTEGRITY_FAILED` warning to
  stderr); they refused it before. `--resume` never reuses such a run.
- `compare STUDY --project` and `report` give a study one freshness record against the current Project
  (`study_freshness`); samples are labelled `HISTORICAL` only when the study is.
- `cases verify` and `contrib case` run each case once with `os-optics project run`. `expected.json` schema 2
  records the execution outcome and every target assessment in place of the aggregate status; a schema 1 file is
  refused with `EXPECTED_SCHEMA` (`cases verify NAME --update-expected` writes the new one). `contrib case`
  packages a `PARTIAL` run with the warning `RUN_EXECUTION_PARTIAL`.

Studies saved by 0.3.0 are still read by `plot`, `report`, `compare` and `present`; they hold no plan, so run them
again to use `--resume`.

## 0.3.0

Requires `os-optics>=0.13,<0.14`; verified with os-optics 0.13.0 and catalog release 2026-10-28.1.

### Added

- `os-optics-kit present`: presentation material from saved results, with themes, languages, figure sizes and
  display units from a saved configuration; an offline gallery and a portable bundle; comparison figures and
  multi-panel layouts; orbit, camera-transition and parameter-sample animations.
- Skill `present`: prepare, restyle and hand over presentation assets.

### Fixed

- Scheduled compatibility checks no longer open duplicate observation issues.

## 0.2.0

Requires `os-optics>=0.13,<0.14`; verified with os-optics 0.13.0 and catalog release 2026-10-28.1.

### Changed

- Moved to os-optics 0.13. `summary`, `edit --run` and the studies read 0.13.0 results: a partial run is
  `UNKNOWN` (exit 2) even when its targets are met; a failed or cancelled run is an error (exit 4).
- Cases verified against os-optics 0.13.0; their results equal those of 0.12.0.
- Runs made with os-optics 0.12.0 read as not current: run the Project again.

### Added

- `render`: the Optical Simulation View at a chosen camera pose (`os-optics-kit[render]`), with the skill `render`.

## 0.1.0

Requires `os-optics>=0.12,<0.13`; verified with os-optics 0.12.0 and catalog release 2026-10-28.1.

First release: tools (Project edits, parameter sweeps, run comparison, plots, reports, sensitivity and tolerance
studies, catalog candidates, measurement processing), 10 engine-reproducible cases, 13 agent skills with a
machine-readable index, and `os-optics-kit contrib case` / `contrib tool`.
