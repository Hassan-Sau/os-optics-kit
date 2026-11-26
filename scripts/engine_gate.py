"""CI gate: which os-optics release does the engine job test, and is it installable from the configured index?

python scripts/engine_gate.py                       # pick the engine under test from baseline.json, resolve it exactly
python scripts/engine_gate.py moved BASE_REF        # exit 1 when BASE_REF..HEAD moves the verified engine or catalog
python scripts/engine_gate.py installed REPORT --expect VERSION [--expect-sha256 DIGEST]
                                                    # the engine check_alignment.py installed, in the job summary

The engine under test is ``engine.candidate_version`` of baseline.json while one is set (a required engine that is
being verified before ``engine.verified_version`` moves to it), otherwise ``engine.verified_version``. It must lie in
``engine.requirement``: the kit refuses any other engine (ENGINE_UNSUPPORTED). The gate resolves exactly
``os-optics==VERSION`` with pip --dry-run (the index is pip's own configuration: PIP_INDEX_URL, PIP_EXTRA_INDEX_URL,
pip.conf) and writes ``engine_candidate=present|missing``, ``version``, ``role``, ``filename`` and ``sha256`` to
$GITHUB_OUTPUT; the engine job passes that version to check_alignment.py (``--engine VERSION``), so the job installs
exactly the engine the gate reported, never the newest release of the range or the previous verified engine.

When the version is not installable the gate prints ENGINE_CANDIDATE_NOT_PUBLISHED, writes a job-summary line and
exits 0: the engine jobs are skipped visibly, every other job still runs. Exit 1 only when the gate itself cannot work
(an unreadable or inconsistent baseline.json, a pip failure other than "not found").
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "ENGINE_CANDIDATE_NOT_PUBLISHED"
MOVED = "BASELINE_MOVED_WITHOUT_ENGINE"
MISMATCH = "ENGINE_INSTALLED_MISMATCH"
VERSION = re.compile(r"\d+(?:\.\d+)+(?:a\d+|b\d+|rc\d+|\.post\d+|\.dev\d+)?")
SHA256 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _specifier_set(requirement: str):
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError:  # the gate job has a bare interpreter; pip vendors packaging
        from pip._vendor.packaging.specifiers import SpecifierSet  # type: ignore[no-redef]
    return SpecifierSet(requirement)


def engine_under_test(document: dict) -> dict:
    """The engine CI tests for a baseline.json document: the candidate while one is set, else the verified engine."""
    engine = document.get("engine")
    if not isinstance(engine, dict):
        raise ValueError("baseline.json: engine must be an object")
    distribution, requirement = engine.get("distribution"), engine.get("requirement")
    if not isinstance(distribution, str) or not NAME.fullmatch(distribution):
        raise ValueError(f"baseline.json: engine.distribution {distribution!r} is not a distribution name")
    if not isinstance(requirement, str) or not requirement:
        raise ValueError("baseline.json: engine.requirement must be a version specifier")
    verified, candidate = engine.get("verified_version"), engine.get("candidate_version")
    role, version = ("candidate", candidate) if candidate is not None else ("verified", verified)
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"baseline.json: engine.{role}_version {version!r} is not an exact release")
    if version not in _specifier_set(requirement):
        raise ValueError(f"baseline.json: engine.{role}_version {version} is outside engine.requirement "
                         f"{requirement}; the kit refuses that engine (ENGINE_UNSUPPORTED)")
    return {"distribution": distribution, "requirement": requirement, "version": version, "role": role,
            "verified": verified, "candidate": candidate, "spec": f"{distribution}=={version}"}


def read_baseline(root: Path = ROOT) -> dict:
    return json.loads((root / "baseline.json").read_text(encoding="utf-8"))


def requirement(root: Path = ROOT) -> str:
    """The exact requirement of the engine under test, e.g. ``os-optics==0.14.0``."""
    return engine_under_test(read_baseline(root))["spec"]


def resolve(spec: str, run=subprocess.run) -> dict | None:
    """What pip would install for ``spec`` (version, artifact URL and SHA-256), or None when the index has nothing."""
    match = NAME.match(spec)
    if not match:
        raise ValueError(f"not a requirement: {spec!r}")
    name = _normal(match.group(0))
    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(scratch) / "report.json"
        done = run([sys.executable, "-m", "pip", "install", "--dry-run", "--no-deps", "--ignore-installed",
                    "--quiet", "--disable-pip-version-check", "--report", str(report_path), spec],
                   capture_output=True, text=True)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    for item in report.get("install", []):
        metadata = item.get("metadata", {})
        if _normal(metadata.get("name", "")) == name:
            download = item.get("download_info") or {}
            url = download.get("url")
            digest = ((download.get("archive_info") or {}).get("hashes") or {}).get("sha256")
            return {"version": metadata["version"], "url": url,
                    "filename": url.rsplit("/", 1)[-1] if isinstance(url, str) else None,
                    "sha256": digest if isinstance(digest, str) and SHA256.fullmatch(digest) else None}
    text = (done.stderr or "") + (done.stdout or "")
    if "No matching distribution" in text or "Could not find a version" in text:
        return None
    raise RuntimeError(f"pip could not resolve {spec} (exit {done.returncode}): {text.strip()[-500:]}")


def _append(path: Path | None, text: str) -> None:
    if path:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


def gate(args) -> int:
    try:
        if args.requirement:
            selected = {"spec": args.requirement, "version": None, "role": "requested", "verified": None,
                        "candidate": None}
        else:
            selected = engine_under_test(read_baseline())
        found = resolve(selected["spec"])
        if found and selected["version"] and found["version"] != selected["version"]:
            raise RuntimeError(f"pip resolved {selected['spec']} to {found['version']}")
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    version = found["version"] if found else selected["version"]
    state = "present" if found else "missing"
    _append(args.github_output,
            f"engine_candidate={state}\nversion={version or ''}\nrole={selected['role']}\n"
            f"filename={(found or {}).get('filename') or ''}\nsha256={(found or {}).get('sha256') or ''}\n")
    role = {"candidate": f"engine.candidate_version; engine.verified_version is {selected['verified']}",
            "verified": "engine.verified_version"}.get(selected["role"], "requested")
    if found is None:
        message = (f"{MARKER}: {selected['spec']} ({role}) is not installable from the configured index. Engine jobs "
                   "are skipped; they run with no change once it is published.")
        print(message, file=sys.stderr)
        _append(args.summary, message + "\n")
    else:
        _append(args.summary, f"### Engine under test: os-optics {version}\n\n"
                              f"Selected from baseline.json ({role}); the engine job installs exactly "
                              f"`{selected['spec']}`.\n\n"
                              f"| Resolved artifact | SHA-256 |\n| --- | --- |\n"
                              f"| {found.get('filename') or '-'} | {found.get('sha256') or '-'} |\n")
    print(json.dumps({"engine_candidate": state, "requirement": selected["spec"], "version": version,
                      "role": selected["role"], "sha256": (found or {}).get("sha256"),
                      "url": (found or {}).get("url")}, separators=(",", ":")))
    return 0


# ----------------------------------------------------------------------------------------------- moved

def baseline_moves(base: dict, head: dict) -> list[str]:
    """The verified baseline fields of baseline.json that differ between two documents.

    Naming (or changing) a candidate engine alone does not move the verified baseline: such a pull request may be
    prepared before the candidate is published (docs/engine-alignment.md, "Move the kit to a new engine minor").
    """
    def fields(document: dict) -> dict:
        return {"engine.verified_version": (document.get("engine") or {}).get("verified_version"),
                "catalog": document.get("catalog")}
    old, new = fields(base), fields(head)
    return [name for name in old if old[name] != new[name]]


def _show(ref: str) -> dict:
    done = subprocess.run(["git", "show", f"{ref}:baseline.json"], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def moved(args) -> int:
    """Exit 1 when BASE..HEAD moves the verified engine or the catalog (used when the engine is missing)."""
    changes = baseline_moves(_show(args.base), _show(args.head))
    if not changes:
        print(json.dumps({"moved": []}))
        return 0
    print(f"::error title={MOVED}::baseline.json moves {', '.join(changes)}, but the engine under test is not "
          "installable; the cases cannot be verified on it")
    print(json.dumps({"moved": changes}))
    return 1


# ----------------------------------------------------------------------------------------------- installed

def installed_problems(report: dict, expect: str, expect_sha256: str | None) -> list[str]:
    source = report.get("engine_source") or {}
    problems = []
    if report.get("installed_version") != expect:
        problems.append(f"installed os-optics {report.get('installed_version') or 'nothing'}, the gate selected "
                        f"{expect}")
    if expect_sha256 and source.get("sha256") != expect_sha256:
        problems.append(f"installed artifact SHA-256 {source.get('sha256') or 'unknown'}, the gate resolved "
                        f"{expect_sha256}")
    if source.get("mode", "pypi") == "pypi" and not source.get("publication_verified"):
        problems.append("the installed artifact is not established as the PyPI release")
    return problems


def installed(args) -> int:
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report = {}
        reason = f"no alignment report ({type(exc).__name__}: {exc})"
    else:
        reason = None
    source = report.get("engine_source") or {}
    problems = [reason] if reason else installed_problems(report, args.expect, args.expect_sha256)
    rows = [("Selected by the gate", args.expect), ("Installed", report.get("installed_version")),
            ("Wheel", source.get("filename")), ("SHA-256", source.get("sha256")), ("URL", source.get("url")),
            ("From PyPI", "yes" if source.get("publication_verified") else "no")]
    lines = [f"### Engine installed: os-optics {report.get('installed_version') or 'none'}", "",
             "| | |", "| --- | --- |"] + [f"| {label} | {value if value else '-'} |" for label, value in rows]
    if problems:
        lines += ["", f"**{MISMATCH}**: " + "; ".join(problems)]
        print(f"::error title={MISMATCH}::" + "; ".join(problems))
    _append(args.summary, "\n".join(lines) + "\n\n")
    print(json.dumps({"installed_version": report.get("installed_version"), "sha256": source.get("sha256"),
                      "expected": args.expect, "problems": problems}, separators=(",", ":")))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github-output", type=Path, default=os.environ.get("GITHUB_OUTPUT") or None)
    parser.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    parser.add_argument("--requirement", help="resolve this specifier instead of the engine under test (local checks)")
    commands = parser.add_subparsers(dest="command")
    moved_parser = commands.add_parser("moved", help="does BASE..HEAD move the verified engine or the catalog?")
    moved_parser.add_argument("base")
    moved_parser.add_argument("head", nargs="?", default="HEAD")
    installed_parser = commands.add_parser("installed", help="report and check the engine check_alignment installed")
    installed_parser.add_argument("report", type=Path)
    installed_parser.add_argument("--expect", required=True, help="the version the gate selected")
    installed_parser.add_argument("--expect-sha256", help="the artifact SHA-256 the gate resolved")
    args = parser.parse_args(argv)
    if args.command == "installed":
        if not VERSION.fullmatch(args.expect):
            parser.error("--expect takes an exact version")
        if args.expect_sha256 is not None and args.expect_sha256 != "" and not SHA256.fullmatch(args.expect_sha256):
            parser.error("--expect-sha256 takes 64 lower-case hexadecimal characters")
        args.expect_sha256 = args.expect_sha256 or None
        return installed(args)
    if args.command == "moved":
        return moved(args)
    return gate(args)


if __name__ == "__main__":
    raise SystemExit(main())
