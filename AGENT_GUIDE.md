# Agent guide

os-optics-kit: tools, engine-reproducible cases and skills for the OS Optics engine (`os-optics`).
Install with `python -m pip install "os-optics[protected]" os-optics-kit` (Python 3.11+); kit 0.4.x
requires `os-optics>=0.13,<0.14`. Engine guide:
[os-optics AGENT_GUIDE](https://github.com/JunLin615/os-optics/blob/main/AGENT_GUIDE.md).

## Load the skills

```bash
os-optics-kit skills list
os-optics-kit skills show NAME [PAGE]
os-optics-kit skills install --dest DIR
```

`skills list` prints the index (name, description, command, requirement) as compact JSON. Read one
skill before using its command; `skills show NAME PAGE` prints a further page it names. `skills install` copies the skill directories and `skills.json` into
DIR, the skills directory of your agent host; it never overwrites a different file without `--force`.

## Route by task

| Task | Skill / command |
| --- | --- |
| Start from a worked design | `run-case`: `os-optics-kit cases list`, `cases copy NAME --dest .`, `cases verify NAME` |
| Read a run's outcome | `summary RUN` |
| Discover IDs, units, axes; edit by ID | `edit P.json --describe --ids ID`; `edit P.json --edits OPS.json --out NEW.json --run RUN` |
| Explore a parameter grid | `sweep SPEC.json --base P.json --out STUDY` |
| Explain what an edit changed | `compare RUN_A RUN_B --project CURRENT.json --out CMP` |
| Plots and a report | `plot sweep STUDY --y target:ID`; `report STUDY --project CURRENT.json` |
| Reproducible 3D figures | `render RUN --out FIGURES --camera 55,22 --background transparent` |
| Presentation material | `present` skill: `present init RUN STUDY --out DIR`; `present build DIR/presentation.json` |
| Restyle without rerunning | `present build DIR/presentation.json --set theme.language=zh-CN --set theme.font_scale=1.3` |
| Compare saved alternatives | `present init RUN_A RUN_B --labels A,B --out DIR` (target chart, shared-framing views) |
| Gallery or bundle handoff | `present list DIR`; `present bundle DIR --out X.zip --only ID,...` |
| Orbit, detail move, sample playback | `present init ... --animate`; fields: `present settings` |
| Sensitivity, tolerance | `sensitivity SPEC.json --out DIR`; `tolerance SPEC.json --out DIR` |
| Many independent runs, in parallel, resumable | `study` skill: `sweep SPEC.json --out S --workers 4`; `study run S --resume`; `study candidates P1.json P2.json --out C` |
| Catalog parts | `candidates --category ... --wavelength-nm ...` |
| Detector CSV data | `measurements DATA.csv --x ... --signal ... --out DIR` |
| Share a design or helper | `contrib-case`, `contrib-tool` |

Each command is `os-optics-kit <command>`; `--help` holds its full specification.
[Workflow recipes](docs/workflow-recipes.md) give spec keys, outputs and error codes.
Cases `expansion-focus`, `polarization-control` and `mirror-alignment` include a parameterized
`workflow.py` (`--list-parameters`, unit-bearing inputs such as `--gap-error-mm 250um`,
`--study NAME=START..STOP:COUNT`, `--no-studies`).

## What the outputs state

A run states separate facts: `outcome.execution` (`COMPLETED`, or `PARTIAL` with the budgets it
reached), each quantity's state (a `BOUNDED` quantity is an interval), each target's assessment
(`MET`, `UNMET`, `INDETERMINATE`, `NOT_APPLICABLE`) and `integrity` (whether its files match their
hashes). A Project without targets has no assessed requirement.

A sample's class (`PASS`, `FAIL`, `OUT_OF_MODEL`, `UNKNOWN`, `NOT_APPLICABLE`, `ERROR`) is the class
of its selected targets' assessments. A study table holds every requested sample; each target value
has a `value_kind` (`EXACT`, `BOUNDED`, ...). `objective_valid` and `feasible` are separate fields;
`NO_FEASIBLE`: no sample meets the constraints. `pass_fraction` divides by all requested samples,
`pass_fraction_among_evaluable` by PASS + FAIL; a tolerance fraction counts simulated samples under
the declared distributions.

Saved results keep their identities after edits. `compare` and `os-optics results freshness RUN
--project P` report `CURRENT`, `HISTORICAL` or `UNDETERMINED` with reasons (`PHYSICS_CHANGED`,
`TARGETS_CHANGED`, ...); a historical result describes the earlier design. A study has one
freshness record.

Exit codes: 0 completed or PASS; 1 FAIL, `NO_FEASIBLE`, `NO_MATCH` or a case mismatch; 2 UNKNOWN or
`INCOMPLETE`; 3 OUT_OF_MODEL (a `PARTIAL` run exits by its targets); 4 refused input, with a JSON
error on stderr; 130 a cancelled study or Ctrl-C (`study run DIR --resume` continues a study). Stdout is compact JSON;
details stay in files.

## Inputs and recovery

Establish wavelength (nm), power (W), Gaussian 1/e² intensity radius and positions (mm), object IDs,
poses and the user's actual target bounds. Coordinates are right-handed; quaternions are
`[w, x, y, z]`. Read `local_axes` before moving along a local axis.

Inputs are never modified; an existing output is refused (`OUTPUT_EXISTS`) unless `--overwrite`.
Invalid edits are refused atomically. Repeating an identical `edit` returns `already_applied`;
`--expect-base-hash` refuses a changed base. A study's optional `max_evaluations` caps its runs; the
Project's compute budget caps the work inside each run. Time a pilot before a
large study and choose a worker budget from it (`skills show study recipe.md`): one study per batch,
`--workers N` (`auto` assumes 0.2 s per sample of a new plan; give N for heavier samples),
`--time-limit-s` / `--max-memory-mib` per evaluation, workers x threads within the CPUs.

## Contribute

`os-optics-kit skills show contrib-case` or `contrib-tool`, then `os-optics-kit contrib case
P.json --name NAME --dry-run`. The user's own GitHub account opens the pull request (`--open-pr`);
without it the command leaves a bundle with the patch. See
[contributing with an agent](docs/contributing-with-an-agent.md).
