"""A custom script that consumes the evaluation adapter directly: scan the refocus offset of the expander/focus
design and print every candidate (valid or not) as JSON. Run: python cases/expansion-focus/adapter_example.py
Guide: docs/en/evaluation-adapter.md."""

from __future__ import annotations

import json
from pathlib import Path

from os_optics_kit.study.evaluation_adapter import Adapter, Constraint, Objective, Problem, Variable

HERE = Path(__file__).resolve().parent
REFOCUS = [{"op": "transform", "selector": {"ids": ["focus"]}, "frame": "design", "translation_mm": ["$value", 0, 0]}]


def main() -> None:
    problem = Problem(
        project=json.loads((HERE / "project.json").read_text(encoding="utf-8")),
        variables=[Variable("refocus", REFOCUS, (-2.0, 2.0), unit="mm", scale=0.1)],
        objectives=[Objective("spot", {"target": "spot_radius"}, scale=0.02)],
        constraints=[Constraint("waist", {"target": "waist_location"}, minimum=-0.5, maximum=0.5, scale=0.5)])
    adapter = Adapter(problem)
    candidates = [adapter.evaluate({"refocus": x}).as_dict() for x in (-0.5, 0.0, 0.5, 3.0)]
    print(json.dumps({"residual_names": problem.residual_names, "candidates": candidates}, default=str))


if __name__ == "__main__":
    main()
