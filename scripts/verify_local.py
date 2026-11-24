"""Local verification of the kit against an engine wheel, on every Python version of baseline.json.

python scripts/verify_local.py --engine-wheel os_optics-X.Y.Z-py3-none-any.whl [--catalog-file RELEASE.json] \
    --out run-local

CI cannot install an unpublished engine, so this is the proof for the engine path. For each Python version in
baseline.json (default 3.11 and 3.14) it runs scripts/check_alignment.py under that interpreter: a clean virtual
environment, the kit from this checkout, the engine from the given wheel, the catalog file imported into a private
store (OPENSCI_CATALOG_DIR under --out) when given, then the full test suite and every case. It prints one summary:
engine version and build, catalog release and schema, case count and failures, per Python version.

Interpreters are found with ``uv python find X.Y`` unless given with --python X.Y=PATH. Exit 0 when every version
passes, 1 when any check fails, 2 when an interpreter is missing. Nothing in the checkout is changed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def python_versions(root: Path = ROOT) -> list[str]:
    return list(json.loads((root / "baseline.json").read_text(encoding="utf-8"))["python"])


def find_interpreter(version: str, given: dict[str, str], run=subprocess.run) -> str | None:
    if version in given:
        return given[version]
    uv = shutil.which("uv")
    if uv is None:
        return None
    done = run([uv, "python", "find", version], capture_output=True, text=True)
    path = (done.stdout or "").strip().splitlines()
    return path[-1] if done.returncode == 0 and path else None


def engine_identity(venv_python: str, run=subprocess.run) -> dict:
    """Engine version, build and schema versions as the installed engine states them."""
    probe = ("import json, os_optics, os_optics._version as v; "
             "print(json.dumps({k.lower(): getattr(v, k) for k in dir(v) "
             "if k.isupper() and isinstance(getattr(v, k), str)} | {'version': os_optics.__version__}))")
    done = run([venv_python, "-c", probe], capture_output=True, text=True)
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"error": (done.stderr or "").strip()[-300:]}


def case_outcomes(report: dict) -> dict:
    """Case count and failing cases from the cases-verify row of an alignment report."""
    row = next((c for c in report.get("checks", []) if c.get("name") == "cases-verify"), None)
    if row is None:
        return {"cases": None, "failed": [], "note": "cases-verify did not run"}
    try:
        document = json.loads(Path(row["stdout"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return {"cases": None, "failed": [], "note": f"cases-verify {row.get('status')}: no JSON result"}
    items = document.get("cases") or document.get("results") or []
    if isinstance(items, dict):
        items = [dict(value, name=key) for key, value in items.items()]
    failed = [item.get("name") or item.get("case") for item in items
              if str(item.get("status", "")).upper() not in ("MATCH", "PASS")]
    return {"cases": len(items), "failed": failed, "status": document.get("status")}


def summarize(version: str, out: Path, report: dict, identity: dict) -> dict:
    checks = report.get("checks", [])
    catalog = report.get("catalog_source", {})
    return {
        "python": report.get("python_version") or version,
        "status": report.get("status"),
        "engine_version": report.get("installed_version") or identity.get("version"),
        "engine_build": identity.get("build_id") or identity.get("engine_build_id"),
        "schema": {key: value for key, value in identity.items() if key.endswith("schema_version")},
        "catalog_release": catalog.get("catalog_version") or catalog.get("release"),
        "catalog_schema": catalog.get("schema_version"),
        "checks": len(checks),
        "failed_checks": [c["name"] for c in checks if c.get("status") == "FAIL"],
        **case_outcomes(report),
        "report": str(out / "report.json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine-wheel", type=Path, required=True)
    parser.add_argument("--catalog-file", type=Path, help="import this catalog release into a private store")
    parser.add_argument("--catalog-sha256", help="published SHA-256 of --catalog-file (when not the baseline's)")
    parser.add_argument("--python", action="append", default=[], metavar="X.Y=PATH")
    parser.add_argument("--out", type=Path, default=Path("run-local"))
    args = parser.parse_args(argv)
    given = dict(item.split("=", 1) for item in args.python)
    wheel = args.engine_wheel.resolve()
    rows, missing = [], []
    for version in python_versions():
        interpreter = find_interpreter(version, given)
        if interpreter is None:
            missing.append(version)
            continue
        out = (args.out / f"py{version}").resolve()
        command = [interpreter, str(ROOT / "scripts/check_alignment.py"), "--engine-wheel", str(wheel),
                   "--optional", "all", "--out", str(out)]
        if args.catalog_file:
            command += ["--catalog-file", str(args.catalog_file.resolve())]
        if args.catalog_sha256:
            command += ["--catalog-sha256", args.catalog_sha256]
        env = dict(os.environ, OPENSCI_CATALOG_DIR=str(out / ".catalog"), OPENSCI_CATALOG_CHECK_INTERVAL="0")
        print(f"[verify-local] Python {version}: {interpreter}", file=sys.stderr, flush=True)
        subprocess.run(command, cwd=ROOT, env=env)
        try:
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report = {"status": "FAIL", "checks": [{"name": "report", "status": "FAIL", "error": str(exc)}]}
        venv_python = out / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        identity = engine_identity(str(venv_python)) if venv_python.exists() else {}
        rows.append(summarize(version, out, report, identity))
    summary = {"schema": "os-optics-kit.verify-local", "status": "PASS", "python": rows, "missing_python": missing}
    if missing:
        summary["status"] = "INCOMPLETE"
    elif any(row["status"] != "PASS" or row["failed"] for row in rows):
        summary["status"] = "FAIL"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for row in rows:
        print(f"Python {row['python']}: {row['status']} | engine {row['engine_version']} build {row['engine_build']} "
              f"| catalog {row['catalog_release']} schema {row['catalog_schema']} | cases {row['cases']} "
              f"failed {row['failed'] or 'none'} | failed checks {row['failed_checks'] or 'none'}")
    for version in missing:
        print(f"Python {version}: MISSING interpreter (pass --python {version}=PATH)")
    print(json.dumps({"status": summary["status"]}))
    return {"PASS": 0, "FAIL": 1}.get(summary["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
