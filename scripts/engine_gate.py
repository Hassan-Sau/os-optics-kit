"""CI gate: is the engine range of baseline.json installable from the configured package index?

python scripts/engine_gate.py            # resolves "os-optics<engine.requirement>" with pip --dry-run

The index is pip's own configuration (PIP_INDEX_URL, PIP_EXTRA_INDEX_URL, pip.conf), so pointing the gate at another
index needs no code change. Writes ``engine_candidate=present|missing`` and ``version=X`` to $GITHUB_OUTPUT. When the
range cannot be resolved it prints ENGINE_CANDIDATE_NOT_PUBLISHED, writes a job-summary line and exits 0: the engine
jobs are skipped visibly, every other job still runs. Exit 1 only when the gate itself cannot work.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "ENGINE_CANDIDATE_NOT_PUBLISHED"


def requirement(root: Path = ROOT) -> str:
    engine = json.loads((root / "baseline.json").read_text(encoding="utf-8"))["engine"]
    return f"{engine['distribution']}{engine['requirement']}"


def resolve(spec: str, run=subprocess.run) -> str | None:
    """The version pip would install for ``spec``, or None when nothing in the index satisfies it."""
    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(scratch) / "report.json"
        done = run([sys.executable, "-m", "pip", "install", "--dry-run", "--no-deps", "--ignore-installed",
                    "--quiet", "--disable-pip-version-check", "--report", str(report_path), spec],
                   capture_output=True, text=True)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    for item in report.get("install", []):
        if item.get("metadata", {}).get("name", "").lower().replace("_", "-") == spec.split("<")[0].split(">")[0]:
            return item["metadata"]["version"]
    text = (done.stderr or "") + (done.stdout or "")
    if "No matching distribution" in text or "Could not find a version" in text:
        return None
    raise RuntimeError(f"pip could not resolve {spec} (exit {done.returncode}): {text.strip()[-500:]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github-output", type=Path, default=os.environ.get("GITHUB_OUTPUT") or None)
    parser.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    parser.add_argument("--requirement", help="resolve this specifier instead of baseline.json's (local checks only)")
    args = parser.parse_args(argv)
    try:
        spec = args.requirement or requirement()
        version = resolve(spec)
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    state = "present" if version else "missing"
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"engine_candidate={state}\nversion={version or ''}\n")
    if version is None:
        message = (f"{MARKER}: {spec} is not installable from the configured index. Engine jobs are skipped; "
                   "they run with no change once it is published.")
        print(message, file=sys.stderr)
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as summary:
                summary.write(message + "\n")
    print(json.dumps({"engine_candidate": state, "requirement": spec, "version": version}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
