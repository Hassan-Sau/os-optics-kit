"""Tests for ``os-optics-kit view``: read-only snapshots, freshness, live SSE follow, corruption recovery and CLI."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

from os_optics_kit import cli
from os_optics_kit.viewer import (
    LocalViewerServer,
    SnapshotError,
    SnapshotTransientError,
    ViewerSession,
    build_snapshot,
    resolve_target,
)
from tests.workflow_fixtures import KIT, ROOT, TempDir, catalog_available, quiet_environment, requires_engine

DETECTOR_CASE_PROJECT = ROOT / "cases" / "detector-measurements" / "project.json"
BEAM_EXPANDER_PROJECT = ROOT / "cases" / "beam-expander" / "project.json"


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_hashes(directory: Path) -> dict[str, str]:
    return {
        p.relative_to(directory).as_posix(): _sha256_bytes(p)
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _detector_base() -> dict:
    return json.loads(DETECTOR_CASE_PROJECT.read_text(encoding="utf-8"))


def _multichannel_project(*, power_w: float = 0.001, signal_radius_mm: float = 0.4, bs_x_mm: float = 0.0) -> dict:
    """Two-branch (transmitted + reflected) setup with 3 distinct power channels:
    DETECTED (signal active area), OUTSIDE_ACTIVE_AREA (signal clip), ESCAPED (non-absorbing reference).
    """
    doc = _detector_base()
    for comp in doc["components"]:
        if comp["instance_id"] == "SRC":
            comp["source_model"]["power_w"] = power_w
        if comp["instance_id"] == "BS":
            comp["nominal_pose"]["position_mm"][0] = bs_x_mm
    for obs in doc["observations"]:
        if obs["observation_id"] == "reference":
            obs["absorbing"] = False
        if obs["observation_id"] == "signal":
            obs["aperture"]["radius_mm"] = signal_radius_mm
    return doc


def _partial_project() -> dict:
    """Same two-branch setup capped to max_paths=1 so execution is PARTIAL with PATH_LIMIT power channel."""
    doc = _detector_base()
    doc["compute_budget"] = {
        "max_paths": 1,
        "max_trace_steps": 2048,
        "max_interactions_per_path": 64,
        "max_interference_pairs": 256,
    }
    return doc


def _write_json(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run_project(doc: dict, out_dir: Path, *, ray_data: str = "full") -> Path:
    from os_optics.local import run_optical_project

    run_optical_project(doc, output_dir=out_dir, ray_data=ray_data)
    return out_dir


def _run_configured_project(doc: dict, out_dir: Path, *, configuration_ids: list[str] | None = None) -> Path:
    from os_optics.local.configurations import run_configured_project

    run_configured_project(doc, output_dir=out_dir, configuration_ids=configuration_ids)
    return out_dir


def _multiconfig_project(
    *,
    low_power_w: float = 0.001,
    high_power_w: float = 0.003,
    low_signal_x_mm: float = 100.0,
    high_signal_x_mm: float = 120.0,
    include_high: bool = True,
) -> dict:
    """Two-configuration Project (``low`` and optional ``high``) with distinct power, signal plane position,
    and per-configuration applicable targets (``signal_power`` -> low only, ``reference_power`` -> high only).
    """
    base = _detector_base()
    doc = json.loads(json.dumps(base))
    src = next(c for c in doc["components"] if c["instance_id"] == "SRC")
    low_source = json.loads(json.dumps(src["source_model"]))
    low_source["power_w"] = low_power_w
    hi_source = json.loads(json.dumps(src["source_model"]))
    hi_source["power_w"] = high_power_w

    sig_obs = next(o for o in doc["observations"] if o["observation_id"] == "signal")
    low_sig_pose = json.loads(json.dumps(sig_obs["pose"]))
    low_sig_pose["position_mm"][0] = low_signal_x_mm
    hi_sig_pose = json.loads(json.dumps(sig_obs["pose"]))
    hi_sig_pose["position_mm"][0] = high_signal_x_mm

    targets = []
    for target in doc["targets"]:
        tid = target["target_id"]
        if tid == "signal_power":
            target["configuration_ids"] = ["low"]
            targets.append(target)
        elif tid == "reference_power":
            if include_high:
                target["configuration_ids"] = ["high"]
                targets.append(target)
        else:
            targets.append(target)
    doc["targets"] = targets

    configs = [
        {
            "configuration_id": "low",
            "label": "Low power",
            "source_models": [{"instance_id": "SRC", "source_model": low_source}],
            "observation_poses": [{"observation_id": "signal", "pose": low_sig_pose}],
        }
    ]
    if include_high:
        configs.append({
            "configuration_id": "high",
            "label": "High power",
            "source_models": [{"instance_id": "SRC", "source_model": hi_source}],
            "observation_poses": [{"observation_id": "signal", "pose": hi_sig_pose}],
        })
    doc["configurations"] = configs
    return doc


def _http_raw_get(host: str, port: int, path: str) -> tuple[int, bytes, dict[str, str]]:
    """Perform a single non-redirecting HTTP GET via stdlib ``http.client``."""
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, headers
    finally:
        conn.close()


def _http_get_status_json(url: str) -> tuple[int, dict, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, json.loads(resp.read().decode("utf-8")), headers
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, json.loads(exc.read().decode("utf-8")), headers


def _run_view_without_engine(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``os_optics_kit.cli.main(['view', ...])`` with both ``os_optics`` import and dist metadata blocked."""
    script = (
        "import importlib.metadata as md, sys\n"
        "class _Block:\n"
        "  def find_spec(self, fullname, *a):\n"
        "    if fullname == 'os_optics' or fullname.startswith('os_optics.'):\n"
        "      raise ModuleNotFoundError(fullname, name=fullname)\n"
        "sys.meta_path.insert(0, _Block())\n"
        "_orig_version = md.version\n"
        "def _fake_version(name):\n"
        "  if name == 'os-optics':\n"
        "    raise md.PackageNotFoundError(name)\n"
        "  return _orig_version(name)\n"
        "md.version = _fake_version\n"
        "from os_optics_kit.cli import main\n"
        "raise SystemExit(main(['view', *sys.argv[1:]]))\n"
    )
    return subprocess.run([sys.executable, "-c", script, *args], capture_output=True, text=True, timeout=60)


def _http_get_json(url: str) -> tuple[int, dict, dict[str, str]]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, json.loads(resp.read().decode("utf-8")), headers


def _http_get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, resp.read(), headers


class _SSEClient:
    """Minimal background SSE reader using stdlib http.client."""

    def __init__(self, host: str, port: int, path: str = "/api/events") -> None:
        self.events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._conn = http.client.HTTPConnection(host, port, timeout=15)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(path,), daemon=True)
        self._thread.start()

    def _run(self, path: str) -> None:
        try:
            self._conn.request("GET", path, headers={"Accept": "text/event-stream"})
            resp = self._conn.getresponse()
            event_name = "message"
            data_lines: list[str] = []
            while not self._stop.is_set():
                raw = resp.fp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        self.events.put((event_name, json.loads("\n".join(data_lines))))
                    event_name = "message"
                    data_lines = []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
        except OSError:
            return

    def wait_for(self, predicate, *, timeout: float = 10.0) -> tuple[str, dict]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for expected SSE event")
            item = self.events.get(timeout=remaining)
            if predicate(item[0], item[1]):
                return item

    def close(self) -> None:
        self._stop.set()
        try:
            self._conn.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def _replace_dir_from_subprocess(src_dir: Path, dst_dir: Path) -> None:
    """Replace ``dst_dir`` contents with ``src_dir`` from an independent child process."""
    code = (
        "from pathlib import Path; import shutil, sys; "
        "src, dst = Path(sys.argv[1]), Path(sys.argv[2]); "
        "staging = dst.parent / (dst.name + '.staging'); "
        "shutil.rmtree(staging, ignore_errors=True); "
        "shutil.copytree(src, staging); "
        "old = dst.parent / (dst.name + '.old'); "
        "shutil.rmtree(old, ignore_errors=True); "
        "dst.rename(old) if dst.exists() else None; "
        "staging.rename(dst); "
        "shutil.rmtree(old, ignore_errors=True)"
    )
    done = subprocess.run([sys.executable, "-c", code, str(src_dir), str(dst_dir)], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"subprocess replace failed: {done.stderr}")


def _write_text_from_subprocess(path: Path, text: str) -> None:
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
    done = subprocess.run([sys.executable, "-c", code, str(path), text], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"subprocess write failed: {done.stderr}")


class ViewerCLIOfflineTests(unittest.TestCase):
    def test_view_registered_and_help_lazy_imports(self):
        self.assertEqual(cli.COMMANDS["view"][0], "viewer_tool")
        done = _run_view_without_engine("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("usage: os-optics-kit view"))

    def test_non_loopback_host_refused_without_engine(self):
        with TempDir() as tmp:
            p = _write_json(tmp / "p.json", _detector_base())
            done = _run_view_without_engine(str(p), "--host", "0.0.0.0", "--no-open")
            self.assertEqual(done.returncode, 4)
            err = json.loads(done.stderr)["error"]
            self.assertEqual(err["code"], "HOST_NOT_LOOPBACK")

    def test_missing_path_refused_without_engine(self):
        with TempDir() as tmp:
            done = _run_view_without_engine(str(tmp / "missing.json"), "--once")
            self.assertEqual(done.returncode, 4)
            err = json.loads(done.stderr)["error"]
            self.assertEqual(err["code"], "INPUT_NOT_FOUND")

    def test_usage_checks_run_before_engine_check(self):
        with TempDir() as tmp:
            run_dir = tmp / "fake_run"
            run_dir.mkdir()
            done_eval = _run_view_without_engine(str(run_dir), "--evaluate", "--once")
            self.assertEqual(done_eval.returncode, 4)
            self.assertEqual(json.loads(done_eval.stderr)["error"]["code"], "USAGE")

            p = _write_json(tmp / "p.json", _detector_base())
            done_poll = _run_view_without_engine(str(p), "--poll-interval", "0", "--once")
            self.assertEqual(done_poll.returncode, 4)
            self.assertEqual(json.loads(done_poll.stderr)["error"]["code"], "USAGE")

    def test_valid_project_without_engine_reports_missing_engine(self):
        with TempDir() as tmp:
            p = _write_json(tmp / "p.json", _detector_base())
            done = _run_view_without_engine(str(p), "--once")
            self.assertEqual(done.returncode, 4)
            self.assertEqual(done.stdout, "")
            err = json.loads(done.stderr)["error"]
            self.assertEqual(err["code"], "ENGINE_NOT_INSTALLED")


@requires_engine
class ViewerSnapshotAccuracyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet_environment()

    def test_multichannel_run_matches_published_engine_outputs(self):
        from os_optics.results import load_run
        from os_optics.viewer import optical_view_scene

        with TempDir() as tmp:
            run_dir = _run_project(_multichannel_project(), tmp / "run")
            hashes_before = _dir_hashes(run_dir)

            built = build_snapshot(resolve_target(run_dir), revision=1)
            snap = built.snapshot

            # Read-only guarantee: run directory untouched.
            self.assertEqual(_dir_hashes(run_dir), hashes_before)

            results = load_run(run_dir, verify=True)
            engine_power = results.power_budget()
            engine_targets = results.targets()
            engine_monitors = results.monitors()
            engine_paths = results.paths()
            engine_scene = optical_view_scene(run_dir)

            self.assertEqual(snap["result_mode"], "computed_run")
            self.assertTrue(snap["is_computed_result"])
            self.assertEqual(snap["revision"], 1)
            self.assertTrue(snap["snapshot_id"].startswith("rev-1-"))
            self.assertEqual(snap["source"]["kind"], "run")
            self.assertTrue(snap["source"]["read_only"])

            # Power ledger: exact values, units, and 3 distinct sink channels.
            pl = snap["power_ledger"]
            self.assertTrue(pl["available"])
            self.assertEqual(pl["unit"], "W")
            self.assertAlmostEqual(pl["total_input_W"], engine_power["total_input_W"], places=12)
            self.assertAlmostEqual(pl["residual_W"], engine_power["residual_W"], places=12)
            self.assertEqual(pl["conserved"], engine_power["conserved"])
            self.assertEqual(set(pl["total_sinks"].keys()), {"DETECTED", "OUTSIDE_ACTIVE_AREA", "ESCAPED"})
            for ch, val in engine_power["total_sinks"].items():
                self.assertAlmostEqual(pl["total_sinks"][ch], val, places=12)
            self.assertEqual({c["channel"] for c in pl["channels"]}, {"DETECTED", "OUTSIDE_ACTIVE_AREA", "ESCAPED"})
            self.assertEqual(len(pl["entries"]), len(engine_power["entries"]))

            # Multi-branch verification (primary transmitted + reflected child branch).
            self.assertEqual(len(snap["branches"]), len(engine_paths))
            by_branch = {b["path_id"]: b for b in snap["branches"]}
            self.assertEqual(set(by_branch.keys()), {"SRC#0", "SRC#0.0"})
            self.assertEqual(by_branch["SRC#0.0"]["parent_path_id"], "SRC#0")
            self.assertEqual(by_branch["SRC#0.0"]["branch_kind"], "beam_splitter_reflection")
            self.assertEqual(by_branch["SRC#0"]["termination"], "DETECTED")
            self.assertEqual(by_branch["SRC#0.0"]["termination"], "ESCAPED")

            # Execution & target assessments (including UNMET signal_power).
            self.assertEqual(snap["execution"]["outcome"], "COMPLETED")
            self.assertTrue(snap["execution"]["complete"])
            self.assertEqual(snap["execution"]["requirement_outcome"], "FAIL")
            by_target = {t["target_id"]: t for t in snap["targets"]}
            self.assertEqual(by_target["signal_power"]["assessment"], "UNMET")
            self.assertEqual(by_target["reference_power"]["assessment"], "MET")
            self.assertEqual(by_target["signal_beam_radius"]["assessment"], "MET")
            self.assertEqual(by_target["reference_beam_radius"]["assessment"], "MET")
            for raw_t in engine_targets["targets"]:
                self.assertAlmostEqual(by_target[raw_t["target_id"]]["value"], raw_t["value"], places=12)
                self.assertEqual(by_target[raw_t["target_id"]]["unit"], raw_t["unit"])

            # Observation quantities.
            self.assertEqual(len(snap["quantities"]), len(engine_monitors))
            by_obs = {q["observation_id"]: q for q in snap["quantities"]}
            self.assertEqual(by_obs["signal"]["quantity_state"], "KNOWN")
            self.assertAlmostEqual(by_obs["signal"]["total_power_W"], engine_power["total_sinks"]["DETECTED"], places=12)

            # Scene binding matches engine viewer scene.
            self.assertFalse(snap["scene"]["geometry_only"])
            self.assertEqual(snap["scene"]["scene_content_hash"], engine_scene.identity["scene_content_hash"])
            self.assertEqual(snap["scene"]["beam_count"], 2)
            self.assertGreater(snap["scene"]["ray_count"], 0)
            self.assertIsNotNone(built.scene_document)
            self.assertEqual(built.scene_document["schema"], "opensci.local_viewer.scene")

    def test_partial_execution_reports_exhausted_budgets_and_bounded_quantities(self):
        with TempDir() as tmp:
            run_dir = _run_project(_partial_project(), tmp / "partial-run")
            snap = build_snapshot(resolve_target(run_dir), revision=1).snapshot

            self.assertEqual(snap["result_mode"], "computed_run")
            self.assertEqual(snap["execution"]["outcome"], "PARTIAL")
            self.assertFalse(snap["execution"]["complete"])
            self.assertIn("max_paths", snap["execution"]["budgets"]["exhausted"])
            self.assertIn("PATH_LIMIT", snap["power_ledger"]["total_sinks"])

            by_target = {t["target_id"]: t for t in snap["targets"]}
            self.assertEqual(by_target["signal_power"]["assessment"], "INDETERMINATE")
            self.assertEqual(by_target["reference_power"]["assessment"], "INDETERMINATE")

            by_obs = {q["observation_id"]: q for q in snap["quantities"]}
            self.assertEqual(by_obs["signal"]["quantity_state"], "BOUNDED")
            self.assertFalse(by_obs["signal"]["power_complete"])
            self.assertEqual(by_obs["reference"]["quantity_state"], "UNKNOWN")

            notice_codes = {n["code"] for n in snap["notices"]}
            self.assertIn("EXECUTION_PARTIAL", notice_codes)

    def test_project_without_run_is_geometry_preview_never_computed_result(self):
        with TempDir() as tmp:
            project_path = _write_json(tmp / "project.json", _multichannel_project())
            before = _sha256_bytes(project_path)

            built = build_snapshot(resolve_target(project_path), revision=1)
            snap = built.snapshot

            self.assertEqual(_sha256_bytes(project_path), before)
            self.assertEqual(snap["result_mode"], "geometry_preview")
            self.assertFalse(snap["is_computed_result"])
            self.assertTrue(snap["scene"]["geometry_only"])
            self.assertEqual(snap["scene"]["beam_count"], 0)
            self.assertEqual(snap["scene"]["ray_count"], 0)
            self.assertEqual(snap["scene"]["part_count"], 4)
            self.assertIsNotNone(built.scene_document)
            self.assertEqual(built.scene_document["beams"], [])
            self.assertEqual(len(built.scene_document["parts"]), 4)

            self.assertFalse(snap["power_ledger"]["available"])
            self.assertEqual(snap["power_ledger"]["reason"], "NO_RUN")
            self.assertIsNone(snap["execution"])
            self.assertEqual(snap["branches"], [])
            self.assertEqual(snap["freshness"]["state"], "NO_RUN")
            self.assertTrue(all(t["status"] == "NOT_COMPUTED" and t["assessment"] is None and t["value"] is None for t in snap["targets"]))
            self.assertTrue(all(q["quantity_state"] == "UNCOMPUTED" and q["total_power_W"] is None for q in snap["quantities"]))
            self.assertIn("GEOMETRY_PREVIEW_ONLY", {n["code"] for n in snap["notices"]})

    def test_explicit_evaluate_uses_temp_dir_and_never_autoreruns_on_edit(self):
        with TempDir() as tmp:
            project_path = _write_json(tmp / "project.json", _multichannel_project(power_w=0.001))
            before = _sha256_bytes(project_path)

            with ViewerSession(resolve_target(project_path), poll_interval=0.1, evaluate=True) as session:
                built = session.load_initial()
                # Input file untouched, no sidecar files created in tmp.
                self.assertEqual(_sha256_bytes(project_path), before)
                self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["project.json"])

                snap1 = built.snapshot
                self.assertEqual(snap1["result_mode"], "computed_run")
                self.assertTrue(snap1["is_computed_result"])
                self.assertEqual(snap1["source"]["evaluation"]["mode"], "explicit_local_temp")
                self.assertFalse(snap1["source"]["evaluation"]["input_modified"])
                self.assertAlmostEqual(snap1["power_ledger"]["total_input_W"], 0.001, places=12)

                # Edit project.json -> viewer MUST NOT auto-rerun; must transition cleanly to stale_run.
                _write_json(project_path, _multichannel_project(power_w=0.002))
                changed = session.poll_once()
                self.assertTrue(changed)
                env2 = session.state.envelope()
                snap2 = env2["snapshot"]
                self.assertEqual(snap2["revision"], 2)
                self.assertEqual(snap2["result_mode"], "stale_run")
                self.assertFalse(snap2["is_computed_result"])
                self.assertEqual(snap2["freshness"]["state"], "HISTORICAL")
                self.assertFalse(snap2["power_ledger"]["available"])
                self.assertTrue(snap2["source"]["evaluation"]["stale_after_edit"])
                self.assertIn("EVALUATE_NOT_AUTORERUN", {n["code"] for n in snap2["notices"]})


@requires_engine
class ViewerLiveFollowAndResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet_environment()

    def test_live_sse_replacement_corruption_recovery_and_read_only_http(self):
        with TempDir() as tmp:
            run_a_src = _run_project(_multichannel_project(power_w=0.001, signal_radius_mm=0.4, bs_x_mm=0.0), tmp / "src_a")
            run_b_src = _run_project(_multichannel_project(power_w=0.002, signal_radius_mm=1.0, bs_x_mm=15.0), tmp / "src_b")
            watched = tmp / "watched_run"
            shutil.copytree(run_a_src, watched)
            hashes_a = _dir_hashes(watched)

            with ViewerSession(resolve_target(watched), poll_interval=0.08) as session:
                session.load_initial()
                session.start()
                with LocalViewerServer(session, host="127.0.0.1", port=0, heartbeat_interval=2.0) as server:
                    # Verify read-only HTTP endpoints & assets.
                    status, html, _ = _http_get_bytes(server.url)
                    self.assertEqual(status, 200)
                    self.assertIn(b"OS Optics Local Viewer", html)

                    status, health, _ = _http_get_json(f"{server.url}api/health")
                    self.assertEqual(status, 200)
                    self.assertTrue(health["ok"])
                    self.assertTrue(health["read_only"])

                    status, three_js, _ = _http_get_bytes(f"{server.url}assets/three.bundle.js")
                    self.assertEqual(status, 200)
                    self.assertGreater(len(three_js), 10000)

                    status, eng_html, _ = _http_get_bytes(f"{server.url}engine-view/")
                    self.assertEqual(status, 200)
                    self.assertIn(b"viewer.js", eng_html)

                    # Mutating methods must be refused with 405.
                    req = urllib.request.Request(f"{server.url}api/snapshot", data=b"{}", method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req, timeout=5)
                    self.assertEqual(ctx.exception.code, 405)

                    # Connect SSE client and verify initial state (Run A).
                    sse = _SSEClient(server.host, server.port)
                    self.addCleanup(sse.close)
                    ev_name, ev0 = sse.wait_for(lambda name, data: name == "state" and data["revision"] == 1)
                    self.assertEqual(ev0["status"], "ok")

                    _, env1, _ = _http_get_json(f"{server.url}api/snapshot")
                    snap_a = env1["snapshot"]
                    hash_a = snap_a["identity"]["project_hash"]
                    scene_hash_a = snap_a["scene"]["scene_content_hash"]
                    self.assertAlmostEqual(snap_a["power_ledger"]["total_input_W"], 0.001, places=12)
                    self.assertEqual(_dir_hashes(watched), hashes_a)

                    # 1) Separate process atomically replaces Run A -> Run B.
                    _replace_dir_from_subprocess(run_b_src, watched)
                    _, ev_b = sse.wait_for(lambda name, data: name == "snapshot" and data["revision"] >= 2)
                    self.assertEqual(ev_b["status"], "ok")

                    _, env2, _ = _http_get_json(f"{server.url}api/snapshot")
                    snap_b = env2["snapshot"]
                    hash_b = snap_b["identity"]["project_hash"]
                    scene_hash_b = snap_b["scene"]["scene_content_hash"]
                    self.assertNotEqual(hash_b, hash_a)
                    self.assertNotEqual(scene_hash_b, scene_hash_a)
                    self.assertAlmostEqual(snap_b["power_ledger"]["total_input_W"], 0.002, places=12)

                    # Scene document bound to exact same revision & Run B identity (never mixes A and B!).
                    _, scene_doc_b, scene_headers = _http_get_json(f"{server.url}api/scene-document")
                    self.assertEqual(scene_headers["x-viewer-revision"], str(snap_b["revision"]))
                    self.assertEqual(scene_doc_b["identity"]["project_hash"], hash_b)
                    self.assertEqual(scene_doc_b["identity"]["scene_content_hash"], scene_hash_b)

                    # 2) Separate process simulates an interrupted / truncated write of simulation_result.json.
                    _write_text_from_subprocess(watched / "simulation_result.json", '{"truncated": ')
                    _, ev_wait = sse.wait_for(lambda name, data: name == "status" and data["status"] == "waiting")
                    self.assertEqual(ev_wait["revision"], snap_b["revision"])

                    _, env_wait, _ = _http_get_json(f"{server.url}api/snapshot")
                    self.assertEqual(env_wait["status"], "waiting")
                    self.assertIsNone(env_wait["snapshot"])
                    self.assertIsNotNone(env_wait["error"])
                    # Last valid snapshot (Run B) stays intact!
                    self.assertEqual(env_wait["last_valid_snapshot"]["revision"], snap_b["revision"])
                    self.assertEqual(env_wait["last_valid_snapshot"]["identity"]["project_hash"], hash_b)

                    # 3) Separate process writes a cross-run torn mix (Run B results + Run A scene/glb):
                    # viewer MUST stay waiting/refuse mixing A's geometry with B's numbers!
                    shutil.copy2(run_b_src / "simulation_result.json", watched / "simulation_result.json")
                    shutil.copy2(run_a_src / "optical_scene.json", watched / "optical_scene.json")
                    shutil.copy2(run_a_src / "optical_scene.glb", watched / "optical_scene.glb")
                    time.sleep(0.25)
                    _, env_torn, _ = _http_get_json(f"{server.url}api/snapshot")
                    self.assertEqual(env_torn["status"], "waiting")
                    self.assertIsNone(env_torn["snapshot"])
                    self.assertEqual(env_torn["last_valid_snapshot"]["revision"], snap_b["revision"])

                    # 4) Separate process completes valid replacement back to Run A -> recovers cleanly!
                    _replace_dir_from_subprocess(run_a_src, watched)
                    _, ev_rec = sse.wait_for(
                        lambda name, data: name == "snapshot" and data["status"] == "ok" and data["revision"] > snap_b["revision"]
                    )
                    self.assertEqual(ev_rec["status"], "ok")
                    _, env_rec, _ = _http_get_json(f"{server.url}api/snapshot")
                    self.assertEqual(env_rec["status"], "ok")
                    self.assertIsNone(env_rec["error"])
                    self.assertEqual(env_rec["snapshot"]["identity"]["project_hash"], hash_a)
                    self.assertAlmostEqual(env_rec["snapshot"]["power_ledger"]["total_input_W"], 0.001, places=12)
                    self.assertEqual(_dir_hashes(watched), hashes_a)

                    # Verify server closes cleanly even while SSE stream is still connected.
                    t0 = time.monotonic()
                self.assertLess(time.monotonic() - t0, 3.0)

    def test_project_with_run_never_relabels_stale_run_after_project_edit(self):
        with TempDir() as tmp:
            doc_a = _multichannel_project(power_w=0.001)
            doc_b = _multichannel_project(power_w=0.003, bs_x_mm=20.0)
            project_path = _write_json(tmp / "project.json", doc_a)
            run_dir = _run_project(doc_a, tmp / "run")
            run_b_src = _run_project(doc_b, tmp / "run_b_src")

            with ViewerSession(resolve_target(project_path, run=run_dir), poll_interval=0.08) as session:
                snap1 = session.load_initial().snapshot
                self.assertEqual(snap1["result_mode"], "computed_run")
                self.assertEqual(snap1["freshness"]["state"], "CURRENT")
                hash_a = snap1["identity"]["project_hash"]

                # Separate process edits project.json A -> B while run_dir still holds A.
                _write_text_from_subprocess(project_path, json.dumps(doc_b, indent=1, sort_keys=True) + "\n")
                self.assertTrue(session.poll_once())
                snap2 = session.state.envelope()["snapshot"]
                self.assertEqual(snap2["result_mode"], "stale_run")
                self.assertFalse(snap2["is_computed_result"])
                self.assertEqual(snap2["freshness"]["state"], "HISTORICAL")
                self.assertIn("PHYSICS_CHANGED", snap2["freshness"]["reasons"])
                self.assertNotEqual(snap2["identity"]["project_hash"], hash_a)
                self.assertEqual(snap2["historical_run"]["project_hash"], hash_a)
                # Never mixes A's numbers with B's geometry!
                self.assertFalse(snap2["power_ledger"]["available"])
                self.assertEqual(snap2["power_ledger"]["reason"], "STALE_RUN")
                self.assertTrue(snap2["scene"]["geometry_only"])
                self.assertEqual(snap2["scene"]["beam_count"], 0)
                self.assertIsNone(snap2["execution"])

                # Separate process updates run_dir to match B -> atomically returns to CURRENT computed_run!
                _replace_dir_from_subprocess(run_b_src, run_dir)
                self.assertTrue(session.poll_once())
                snap3 = session.state.envelope()["snapshot"]
                self.assertEqual(snap3["result_mode"], "computed_run")
                self.assertTrue(snap3["is_computed_result"])
                self.assertEqual(snap3["freshness"]["state"], "CURRENT")
                self.assertEqual(snap3["identity"]["project_hash"], snap2["identity"]["project_hash"])
                self.assertAlmostEqual(snap3["power_ledger"]["total_input_W"], 0.003, places=12)

    def test_project_only_live_edit_and_truncated_json_recovery(self):
        with TempDir() as tmp:
            doc_a = _multichannel_project(power_w=0.001, bs_x_mm=0.0)
            doc_b = _multichannel_project(power_w=0.001, bs_x_mm=25.0)
            project_path = _write_json(tmp / "project.json", doc_a)

            with ViewerSession(resolve_target(project_path), poll_interval=0.08) as session:
                snap1 = session.load_initial().snapshot
                self.assertEqual(snap1["result_mode"], "geometry_preview")
                hash_a = snap1["identity"]["project_hash"]

                # Truncated write enters waiting state, preserving last_valid_snapshot.
                _write_text_from_subprocess(project_path, '{"schema": "opensci.project", "components": [')
                self.assertTrue(session.poll_once())
                env_wait = session.state.envelope()
                self.assertEqual(env_wait["status"], "waiting")
                self.assertIsNone(env_wait["snapshot"])
                self.assertEqual(env_wait["last_valid_snapshot"]["identity"]["project_hash"], hash_a)
                self.assertEqual(env_wait["error"]["code"], "PROJECT_JSON_INVALID")

                # Valid rewrite of doc_b recovers cleanly to revision 2 with new geometry & hash.
                _write_text_from_subprocess(project_path, json.dumps(doc_b, indent=1, sort_keys=True) + "\n")
                self.assertTrue(session.poll_once())
                env_rec = session.state.envelope()
                self.assertEqual(env_rec["status"], "ok")
                self.assertIsNone(env_rec["error"])
                snap2 = env_rec["snapshot"]
                self.assertEqual(snap2["revision"], 2)
                self.assertEqual(snap2["result_mode"], "geometry_preview")
                self.assertFalse(snap2["is_computed_result"])
                self.assertNotEqual(snap2["identity"]["project_hash"], hash_a)
                bs_part = next(p for p in snap2["scene"]["parts"] if p["id"] == "BS")
                self.assertAlmostEqual(bs_part["position_mm"][0], 25.0, places=4)

    def test_run_dir_with_external_project_flag_tracks_freshness(self):
        with TempDir() as tmp:
            doc_a = _multichannel_project(power_w=0.001)
            doc_b = _multichannel_project(power_w=0.004)
            project_path = _write_json(tmp / "project.json", doc_a)
            run_dir = _run_project(doc_a, tmp / "run")

            snap_cur = build_snapshot(resolve_target(run_dir, project=project_path), revision=1).snapshot
            self.assertEqual(snap_cur["source"]["kind"], "run")
            self.assertEqual(snap_cur["result_mode"], "computed_run")
            self.assertEqual(snap_cur["freshness"]["state"], "CURRENT")

            _write_json(project_path, doc_b)
            snap_stale = build_snapshot(resolve_target(run_dir, project=project_path), revision=2).snapshot
            self.assertEqual(snap_stale["result_mode"], "stale_run")
            self.assertEqual(snap_stale["freshness"]["state"], "HISTORICAL")
            self.assertFalse(snap_stale["power_ledger"]["available"])

    def test_every_accepted_loopback_host_serves_and_each_server_has_its_own_instance(self):
        """``--host`` accepts 127.0.0.1, localhost and ::1; each must actually serve, with a URL that works. Each server
        process carries its own ``instance_id`` so an open page can tell a restarted viewer from the one it followed."""
        import socket

        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
            hosts = ("127.0.0.1", "localhost", "::1")
        except OSError:
            hosts = ("127.0.0.1", "localhost")
        with TempDir() as tmp:
            run_dir = _run_project(_multichannel_project(), tmp / "run")
            instances = set()
            for host in hosts:
                with self.subTest(host=host):
                    with ViewerSession(resolve_target(run_dir), poll_interval=5.0) as session:
                        session.load_initial()
                        with LocalViewerServer(session, host=host, port=0) as server:
                            if host == "::1":
                                self.assertEqual(server.url, f"http://[::1]:{server.port}/")
                            status, health, _ = _http_get_status_json(f"{server.url}api/health")
                            self.assertEqual(status, 200)
                            self.assertTrue(health["ok"])
                            _st, envelope, _h = _http_get_status_json(f"{server.url}api/snapshot")
                            self.assertEqual(envelope["server"]["url"], server.url)
                            self.assertRegex(envelope["server"]["instance_id"], r"^[0-9a-f]{32}$")
                            instances.add(envelope["server"]["instance_id"])
            self.assertEqual(len(instances), len(hosts))

    def test_cli_subprocess_startup_live_api_and_clean_shutdown(self):
        with TempDir() as tmp:
            run_dir = _run_project(_multichannel_project(), tmp / "cli_run")
            hashes_before = _dir_hashes(run_dir)

            # 1) --once mode prints valid envelope JSON and exits 0.
            once = subprocess.run(
                [sys.executable, *KIT, "view", str(run_dir), "--once"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(once.returncode, 0, once.stderr)
            once_env = json.loads(once.stdout)
            self.assertEqual(once_env["status"], "ok")
            self.assertEqual(once_env["snapshot"]["result_mode"], "computed_run")

            # 2) Live server subprocess with --no-open starts, prints READY JSON, serves API, and shuts down cleanly.
            env = dict(os.environ)
            env.setdefault("OPENSCI_CATALOG_CHECK_INTERVAL", "0")
            proc = subprocess.Popen(
                [sys.executable, *KIT, "view", str(run_dir), "--no-open", "--poll-interval", "0.1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                if not line:
                    err_text = proc.stderr.read() if proc.stderr else ""
                    self.fail(f"server produced no startup line; stderr={err_text}")
                ready = json.loads(line)
                self.assertEqual(ready["status"], "READY")
                self.assertTrue(ready["url"].startswith("http://127.0.0.1:"))
                self.assertEqual(ready["result_mode"], "computed_run")

                status, snap_env, _ = _http_get_json(ready["endpoints"]["snapshot"])
                self.assertEqual(status, 200)
                self.assertEqual(snap_env["status"], "ok")
                self.assertEqual(snap_env["snapshot"]["revision"], 1)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            self.assertEqual(_dir_hashes(run_dir), hashes_before)

    def test_on_disk_integrity_failure_never_publishes_and_recovers(self):
        """Fixture A (1 mW, completed, 2 branches) vs Fixture B (3 mW, partial, 1 branch):
        mixing A's ray_data.json into B must NEVER publish ok or overwrite last_valid_snapshot.
        """
        with TempDir() as tmp:
            doc_a = _multichannel_project(power_w=0.001)
            doc_b = _partial_project()
            for comp in doc_b["components"]:
                if comp["instance_id"] == "SRC":
                    comp["source_model"]["power_w"] = 0.003

            run_a = _run_project(doc_a, tmp / "run_a")
            run_b = _run_project(doc_b, tmp / "run_b")
            watched = tmp / "watched"
            shutil.copytree(run_b, watched)
            project_b_path = _write_json(tmp / "project_b.json", doc_b)

            # 1) Valid PARTIAL run B MUST publish ok / computed_run with its real results & limits.
            with ViewerSession(resolve_target(watched), poll_interval=0.08) as session:
                snap1 = session.load_initial().snapshot
                self.assertEqual(snap1["result_mode"], "computed_run")
                self.assertEqual(snap1["execution"]["outcome"], "PARTIAL")
                self.assertAlmostEqual(snap1["power_ledger"]["total_input_W"], 0.003, places=12)
                self.assertEqual(len(snap1["branches"]), 1)
                self.assertTrue(snap1["integrity"]["consistent"])

                # 2) Temporarily overwrite ONLY B's ray_data.json with A's ray_data.json.
                shutil.copy2(run_a / "ray_data.json", watched / "ray_data.json")
                self.assertTrue(session.poll_once())
                env_bad = session.state.envelope()
                self.assertEqual(env_bad["status"], "waiting")
                self.assertIsNone(env_bad["snapshot"])
                self.assertEqual(env_bad["error"]["code"], "RUN_INTEGRITY_FAILED")
                self.assertTrue(env_bad["error"]["details"].get("problems"))
                # Last genuinely verified snapshot (B, rev 1) stays intact!
                self.assertEqual(env_bad["last_valid_snapshot"]["revision"], 1)
                self.assertEqual(env_bad["last_valid_snapshot"]["snapshot_id"], snap1["snapshot_id"])

                # 3) Also check startup / --once with NO prior valid snapshot refuses corrupted run.
                with self.assertRaises(SnapshotTransientError) as ctx:
                    build_snapshot(resolve_target(watched), revision=1)
                self.assertEqual(ctx.exception.code, "RUN_INTEGRITY_FAILED")

                once_bad = subprocess.run(
                    [sys.executable, *KIT, "view", str(watched), "--once"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(once_bad.returncode, 4)
                self.assertEqual(once_bad.stdout, "")
                self.assertEqual(json.loads(once_bad.stderr)["error"]["code"], "RUN_INTEGRITY_FAILED")

                # 4) Also check Project + Run pairing: integrity failure must raise RUN_INTEGRITY_FAILED
                #    instead of silently publishing stale_run!
                with self.assertRaises(SnapshotTransientError) as ctx_pair:
                    build_snapshot(resolve_target(project_b_path, run=watched), revision=1)
                self.assertEqual(ctx_pair.exception.code, "RUN_INTEGRITY_FAILED")

                # 5) Restore valid B ray_data.json -> watcher recovers automatically!
                shutil.copy2(run_b / "ray_data.json", watched / "ray_data.json")
                self.assertTrue(session.poll_once())
                env_rec = session.state.envelope()
                self.assertEqual(env_rec["status"], "ok")
                self.assertIsNone(env_rec["error"])
                self.assertEqual(env_rec["snapshot"]["result_mode"], "computed_run")
                self.assertAlmostEqual(env_rec["snapshot"]["power_ledger"]["total_input_W"], 0.003, places=12)

    def test_snapshot_id_strict_binding_and_eviction_returns_409(self):
        """Every HTTP scene/engine-view request pinned to snapshot_id must return THAT snapshot's
        exact bytes or HTTP 409 SNAPSHOT_SUPERSEDED once evicted — NEVER another snapshot's bytes.
        """
        with TempDir() as tmp:
            runs = [
                _run_project(_multichannel_project(power_w=0.001 * (i + 1), bs_x_mm=float(i * 5)), tmp / f"run_{i}")
                for i in range(5)
            ]
            watched = tmp / "watched"
            shutil.copytree(runs[0], watched)

            with ViewerSession(resolve_target(watched), poll_interval=0.1, max_cached_snapshots=3) as session:
                snap_a = session.load_initial().snapshot
                snap_a_id = snap_a["snapshot_id"]
                hash_a = snap_a["identity"]["project_hash"]
                scene_hash_a = snap_a["scene"]["scene_content_hash"]

                with LocalViewerServer(session, host="127.0.0.1", port=0) as server:
                    # Verify snapshot URLs are strictly bound to snapshot_id.
                    self.assertEqual(snap_a["scene"]["engine_view_url"], f"/engine-view/{snap_a_id}/")
                    self.assertEqual(snap_a["scene"]["document_url"], f"/api/scene-document?snapshot_id={snap_a_id}")

                    # Advance to Run B (rev 2); both A and B are in the bounded cache (size 3).
                    _replace_dir_from_subprocess(runs[1], watched)
                    self.assertTrue(session.poll_once())
                    snap_b = session.state.envelope()["snapshot"]
                    snap_b_id = snap_b["snapshot_id"]
                    hash_b = snap_b["identity"]["project_hash"]
                    self.assertNotEqual(hash_a, hash_b)

                    # Delayed fetch for A's pinned URLs MUST still return A's exact bytes (NEVER B's!).
                    st_a1, view_a, hdr_a1 = _http_get_status_json(f"{server.url}engine-view/{snap_a_id}/view.json")
                    self.assertEqual(st_a1, 200)
                    self.assertEqual(hdr_a1["x-viewer-snapshot-id"], snap_a_id)
                    self.assertEqual(hdr_a1["x-viewer-revision"], "1")
                    self.assertEqual(hdr_a1["cache-control"], "no-store")
                    self.assertEqual(view_a["identity"]["project_hash"], hash_a)
                    self.assertEqual(view_a["identity"]["scene_content_hash"], scene_hash_a)

                    st_a2, doc_a, hdr_a2 = _http_get_status_json(f"{server.url}api/scene-document?snapshot_id={snap_a_id}")
                    self.assertEqual(st_a2, 200)
                    self.assertEqual(hdr_a2["x-viewer-snapshot-id"], snap_a_id)
                    self.assertEqual(doc_a["identity"]["project_hash"], hash_a)

                    st_a_html, html_a, hdr_a_html = _http_get_bytes(f"{server.url}engine-view/{snap_a_id}/")
                    self.assertEqual(st_a_html, 200)
                    self.assertEqual(hdr_a_html["x-viewer-snapshot-id"], snap_a_id)
                    self.assertIn(b"viewer.js", html_a)

                    # Canonical /engine-view/?rev=1 MUST 307-redirect to /engine-view/<rev1_snapshot_id>/
                    # so relative view.json loads rev-1's exact bytes even after rev-2 is published!
                    for redir_path in ("/engine-view/?rev=1", "/engine-view?rev=1", f"/engine-view/?snapshot_id={snap_a_id}"):
                        st_r, _body_r, hdr_r = _http_raw_get(server.host, server.port, redir_path)
                        self.assertEqual(st_r, 307, redir_path)
                        self.assertEqual(hdr_r["location"], f"/engine-view/{snap_a_id}/", redir_path)
                        self.assertEqual(hdr_r["cache-control"], "no-store", redir_path)
                        self.assertEqual(hdr_r["x-viewer-revision"], "1", redir_path)
                        self.assertEqual(hdr_r["x-viewer-snapshot-id"], snap_a_id, redir_path)

                    # Follow the redirect location and verify relative view.json returns rev-1 (A), not rev-2 (B).
                    st_r_html, r_html, r_html_hdr = _http_raw_get(server.host, server.port, f"/engine-view/{snap_a_id}/")
                    self.assertEqual(st_r_html, 200)
                    self.assertEqual(r_html_hdr["x-viewer-snapshot-id"], snap_a_id)
                    self.assertIn(b"viewer.js", r_html)
                    st_r_view, r_view, r_view_hdr = _http_get_status_json(f"{server.url}engine-view/{snap_a_id}/view.json")
                    self.assertEqual(st_r_view, 200)
                    self.assertEqual(r_view_hdr["x-viewer-revision"], "1")
                    self.assertEqual(r_view["identity"]["project_hash"], hash_a)
                    self.assertEqual(r_view["identity"]["scene_content_hash"], scene_hash_a)

                    # Unqualified /engine-view and /engine-view/ 307-redirect to the current active snapshot (B).
                    for active_path in ("/engine-view", "/engine-view/"):
                        st_cur, _body_cur, hdr_cur = _http_raw_get(server.host, server.port, active_path)
                        self.assertEqual(st_cur, 307, active_path)
                        self.assertEqual(hdr_cur["location"], f"/engine-view/{snap_b_id}/", active_path)
                        self.assertEqual(hdr_cur["cache-control"], "no-store", active_path)
                        self.assertEqual(hdr_cur["x-viewer-revision"], "2", active_path)
                        self.assertEqual(hdr_cur["x-viewer-snapshot-id"], snap_b_id, active_path)

                    # Unversioned /engine-view/view.json and unversioned assets are NEVER served.
                    for unversioned in (
                        f"{server.url}engine-view/view.json",
                        f"{server.url}engine-view/index.html",
                        f"{server.url}engine-view/viewer.js",
                    ):
                        with self.subTest(unversioned=unversioned):
                            st_u, body_u, _ = _http_get_status_json(unversioned)
                            self.assertEqual(st_u, 404)
                            self.assertEqual(body_u["error"]["code"], "NOT_FOUND")

                    # B's pinned URL returns B's exact bytes.
                    st_b, view_b, hdr_b = _http_get_status_json(f"{server.url}engine-view/{snap_b_id}/view.json")
                    self.assertEqual(st_b, 200)
                    self.assertEqual(hdr_b["x-viewer-snapshot-id"], snap_b_id)
                    self.assertEqual(view_b["identity"]["project_hash"], hash_b)

                    # Push 3 more updates (revs 3, 4, 5) so rev 1 (A) and rev 2 (B) are evicted from cache of size 3.
                    for idx in (2, 3, 4):
                        _replace_dir_from_subprocess(runs[idx], watched)
                        self.assertTrue(session.poll_once())
                    snap_latest = session.state.envelope()["snapshot"]
                    self.assertEqual(snap_latest["revision"], 5)

                    # The engine's code is not bound to a snapshot: with A and B evicted it is still served, so an
                    # engine page that is still starting after its snapshot was evicted can run and report the
                    # refused scene instead of stalling.
                    st_page, page_html, _ = _http_get_bytes(f"{server.url}engine-view/{snap_latest['snapshot_id']}/")
                    self.assertEqual(st_page, 200)
                    for ref in (b'src="/assets/engine-boot.js"', b'href="/engine-assets/viewer.css"',
                                b'src="/engine-assets/three.bundle.js"', b'src="/engine-assets/viewer.js"'):
                        self.assertIn(ref, page_html)
                    self.assertLess(page_html.index(b"engine-boot.js"), page_html.index(b"/engine-assets/"))
                    for name, ctype in (("viewer.js", "text/javascript"), ("three.bundle.js", "text/javascript"),
                                        ("viewer.css", "text/css"), ("THREE_LICENSE.txt", "text/plain")):
                        with self.subTest(engine_asset=name):
                            st_asset, body_asset, hdr_asset = _http_get_bytes(f"{server.url}engine-assets/{name}")
                            self.assertEqual(st_asset, 200)
                            self.assertTrue(hdr_asset["content-type"].startswith(ctype))
                            self.assertEqual(hdr_asset["cache-control"], "no-store")
                            self.assertNotIn("x-viewer-snapshot-id", hdr_asset)
                            self.assertGreater(len(body_asset), 0)
                    st_boot, _boot_body, hdr_boot = _http_get_bytes(f"{server.url}assets/engine-boot.js")
                    self.assertEqual(st_boot, 200)
                    self.assertTrue(hdr_boot["content-type"].startswith("text/javascript"))
                    for missing in (f"{server.url}engine-assets/index.html", f"{server.url}engine-assets/view.json",
                                    f"{server.url}engine-view/{snap_latest['snapshot_id']}/viewer.js"):
                        with self.subTest(missing=missing):
                            st_m, body_m, _ = _http_get_status_json(missing)
                            self.assertEqual(st_m, 404)
                            self.assertEqual(body_m["error"]["code"], "NOT_FOUND")

                    # Requesting evicted A MUST return HTTP 409 SNAPSHOT_SUPERSEDED (never latest bytes!).
                    for url in (
                        f"{server.url}engine-view/{snap_a_id}/view.json",
                        f"{server.url}engine-view/{snap_a_id}/",
                        f"{server.url}engine-view/?rev=1",
                        f"{server.url}engine-view/?snapshot_id={snap_a_id}",
                        f"{server.url}api/scene-document?snapshot_id={snap_a_id}",
                        f"{server.url}api/scene-document?rev=1",
                        f"{server.url}engine-view/rev-999-unknown/view.json",
                    ):
                        with self.subTest(url=url):
                            st, body, _ = _http_get_status_json(url)
                            self.assertEqual(st, 409)
                            err = body["error"]
                            self.assertEqual(err["code"], "SNAPSHOT_SUPERSEDED")
                            self.assertEqual(err["current_snapshot_id"], snap_latest["snapshot_id"])
                            self.assertEqual(err["current_revision"], 5)

    def test_ray_data_summary_run_succeeds_without_crossing_table(self):
        with TempDir() as tmp:
            run_dir = _run_project(_multichannel_project(), tmp / "summary_run", ray_data="summary")

            # CLI --once must exit 0 (not exit 4 on PUPIL_RAYS_NOT_RECORDED!).
            once = subprocess.run(
                [sys.executable, *KIT, "view", str(run_dir), "--once"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(once.returncode, 0, once.stderr)
            env = json.loads(once.stdout)
            self.assertEqual(env["status"], "ok")
            snap = env["snapshot"]

            self.assertEqual(snap["result_mode"], "computed_run")
            self.assertTrue(snap["is_computed_result"])
            self.assertTrue(snap["power_ledger"]["available"])
            self.assertEqual(len(snap["branches"]), 2)
            self.assertEqual(snap["scene"]["branch_count"], 2)
            self.assertEqual(snap["scene"]["beam_count"], 2)
            self.assertGreater(snap["scene"]["drawable_ray_count"], 0)
            self.assertEqual(snap["scene"]["ray_count"], snap["scene"]["drawable_ray_count"])
            self.assertFalse(snap["scene"]["crossing_table_available"])
            self.assertIsNone(snap["scene"]["crossing_row_count"])
            self.assertEqual(snap["scene"]["ray_data_mode"], "summary")
            self.assertIn("RAY_DATA_SUMMARY_ONLY", {n["code"] for n in snap["notices"]})

    def test_no_observation_plane_run_distinguishes_drawable_rays_from_crossings(self):
        with TempDir() as tmp:
            doc = _detector_base()
            doc["observations"] = []
            doc["targets"] = []
            run_dir = _run_project(doc, tmp / "noobs_run")

            snap = build_snapshot(resolve_target(run_dir), revision=1).snapshot
            self.assertEqual(snap["result_mode"], "computed_run")
            self.assertEqual(snap["scene"]["branch_count"], 2)
            self.assertEqual(snap["scene"]["beam_count"], 2)
            self.assertEqual(snap["scene"]["drawable_ray_count"], 26)
            self.assertEqual(snap["scene"]["ray_count"], 26)
            self.assertTrue(snap["scene"]["crossing_table_available"])
            self.assertEqual(snap["scene"]["crossing_row_count"], 0)

    def test_multi_configuration_project_and_root_run_freshness(self):
        """Verify consistent declaration-order selection, per-configuration freshness, configuration-applied
        previews (3D scene + quantities + applicable targets), subset runs, removed/renamed configurations,
        and manifest-only discovery ignoring leftover user files in old configuration directories.
        """
        with TempDir() as tmp:
            doc = _multiconfig_project(
                low_power_w=0.001,
                high_power_w=0.003,
                low_signal_x_mm=100.0,
                high_signal_x_mm=120.0,
            )
            project_path = _write_json(tmp / "project.json", doc)
            root_run = _run_configured_project(doc, tmp / "cfg_run")

            # 0) Project-only geometry previews apply the selected configuration consistently to 3D scene,
            #    quantities, and applicable targets (default = first declared = 'low').
            snap_proj_only_default = build_snapshot(resolve_target(project_path), revision=1).snapshot
            self.assertEqual(snap_proj_only_default["result_mode"], "geometry_preview")
            self.assertEqual(snap_proj_only_default["source"]["configuration_id"], "low")
            self.assertEqual(snap_proj_only_default["source"]["available_configurations"], ["low", "high"])
            low_scene_sig = next(p for p in snap_proj_only_default["scene"]["parts"] if p["id"] == "signal")
            low_qty_sig = next(q for q in snap_proj_only_default["quantities"] if q["observation_id"] == "signal")
            self.assertAlmostEqual(low_scene_sig["position_mm"][0], 100.0, places=4)
            self.assertAlmostEqual(low_qty_sig["plane"]["origin_mm"][0], 100.0, places=4)
            low_target_ids = {t["target_id"] for t in snap_proj_only_default["targets"]}
            self.assertIn("signal_power", low_target_ids)
            self.assertNotIn("reference_power", low_target_ids)

            snap_proj_only_high = build_snapshot(resolve_target(project_path, configuration="high"), revision=1).snapshot
            self.assertEqual(snap_proj_only_high["result_mode"], "geometry_preview")
            self.assertEqual(snap_proj_only_high["source"]["configuration_id"], "high")
            hi_scene_sig = next(p for p in snap_proj_only_high["scene"]["parts"] if p["id"] == "signal")
            hi_qty_sig = next(q for q in snap_proj_only_high["quantities"] if q["observation_id"] == "signal")
            self.assertAlmostEqual(hi_scene_sig["position_mm"][0], 120.0, places=4)
            self.assertAlmostEqual(hi_qty_sig["plane"]["origin_mm"][0], 120.0, places=4)
            hi_target_ids = {t["target_id"] for t in snap_proj_only_high["targets"]}
            self.assertIn("reference_power", hi_target_ids)
            self.assertNotIn("signal_power", hi_target_ids)

            # 1) view PROJECT --run ROOT_RUN (default selects first declared 'low'; declaration order ['low', 'high']).
            snap_proj_default = build_snapshot(resolve_target(project_path, run=root_run), revision=1).snapshot
            self.assertEqual(snap_proj_default["result_mode"], "computed_run")
            self.assertTrue(snap_proj_default["is_computed_result"])
            self.assertEqual(snap_proj_default["freshness"]["state"], "CURRENT")
            self.assertEqual(snap_proj_default["source"]["available_configurations"], ["low", "high"])
            self.assertEqual(snap_proj_default["source"]["configuration_id"], "low")
            self.assertAlmostEqual(snap_proj_default["power_ledger"]["total_input_W"], 0.001, places=12)

            # 2) view ROOT_RUN --project PROJECT for 'high' -> CURRENT / computed_run (3 mW).
            snap_run_pair = build_snapshot(
                resolve_target(root_run, project=project_path, configuration="high"), revision=2
            ).snapshot
            self.assertEqual(snap_run_pair["result_mode"], "computed_run")
            self.assertEqual(snap_run_pair["freshness"]["state"], "CURRENT")
            self.assertEqual(snap_run_pair["source"]["configuration_id"], "high")
            self.assertAlmostEqual(snap_run_pair["power_ledger"]["total_input_W"], 0.003, places=12)

            # 3) Edit ONLY 'high' (3 mW -> 4 mW):
            #    - 'low' MUST stay CURRENT / computed_run (1 mW ledger & rays)!
            #    - 'high' MUST become HISTORICAL / stale_run with high-applied geometry, plane x=120, high-only targets!
            _write_json(
                project_path,
                _multiconfig_project(low_power_w=0.001, high_power_w=0.004, low_signal_x_mm=100.0, high_signal_x_mm=120.0),
            )
            for label, low_spec, high_spec in (
                (
                    "project+run",
                    resolve_target(project_path, run=root_run, configuration="low"),
                    resolve_target(project_path, run=root_run, configuration="high"),
                ),
                (
                    "run+project",
                    resolve_target(root_run, project=project_path, configuration="low"),
                    resolve_target(root_run, project=project_path, configuration="high"),
                ),
            ):
                with self.subTest(pairing=label):
                    snap_low = build_snapshot(low_spec, revision=3).snapshot
                    self.assertEqual(snap_low["result_mode"], "computed_run")
                    self.assertTrue(snap_low["is_computed_result"])
                    self.assertEqual(snap_low["freshness"]["state"], "CURRENT")
                    self.assertEqual(snap_low["freshness"]["scope"], "configuration")
                    self.assertEqual(snap_low["freshness"]["root_freshness"]["state"], "HISTORICAL")
                    self.assertTrue(snap_low["power_ledger"]["available"])
                    self.assertAlmostEqual(snap_low["power_ledger"]["total_input_W"], 0.001, places=12)
                    self.assertGreater(snap_low["scene"]["beam_count"], 0)

                    snap_stale = build_snapshot(high_spec, revision=4).snapshot
                    self.assertEqual(snap_stale["result_mode"], "stale_run")
                    self.assertFalse(snap_stale["is_computed_result"])
                    self.assertEqual(snap_stale["freshness"]["state"], "HISTORICAL")
                    self.assertIn("PHYSICS_CHANGED", snap_stale["freshness"]["reasons"])
                    self.assertFalse(snap_stale["power_ledger"]["available"])
                    self.assertEqual(snap_stale["power_ledger"]["reason"], "STALE_RUN")
                    self.assertEqual(snap_stale["scene"]["beam_count"], 0)
                    stale_sig_part = next(p for p in snap_stale["scene"]["parts"] if p["id"] == "signal")
                    stale_sig_qty = next(q for q in snap_stale["quantities"] if q["observation_id"] == "signal")
                    self.assertAlmostEqual(stale_sig_part["position_mm"][0], 120.0, places=4)
                    self.assertAlmostEqual(stale_sig_qty["plane"]["origin_mm"][0], 120.0, places=4)
                    stale_targets = {t["target_id"] for t in snap_stale["targets"]}
                    self.assertIn("reference_power", stale_targets)
                    self.assertNotIn("signal_power", stale_targets)

            # 4) Subset run (Project declares 'low' and 'high', run recorded only 'low'):
            _write_json(project_path, doc)
            subset_run = _run_configured_project(doc, tmp / "subset_run", configuration_ids=["low"])
            for label, low_spec, high_spec in (
                (
                    "subset-project+run",
                    resolve_target(project_path, run=subset_run),
                    resolve_target(project_path, run=subset_run, configuration="high"),
                ),
                (
                    "subset-run+project",
                    resolve_target(subset_run, project=project_path, configuration="low"),
                    resolve_target(subset_run, project=project_path, configuration="high"),
                ),
            ):
                with self.subTest(pairing=label):
                    snap_sub_low = build_snapshot(low_spec, revision=5).snapshot
                    self.assertEqual(snap_sub_low["result_mode"], "computed_run")
                    self.assertTrue(snap_sub_low["is_computed_result"])
                    self.assertEqual(snap_sub_low["freshness"]["state"], "CURRENT")
                    self.assertEqual(snap_sub_low["source"]["configuration_id"], "low")
                    self.assertEqual(snap_sub_low["source"]["recorded_configurations"], ["low"])
                    self.assertAlmostEqual(snap_sub_low["power_ledger"]["total_input_W"], 0.001, places=12)

                    snap_sub_high = build_snapshot(high_spec, revision=6).snapshot
                    self.assertEqual(snap_sub_high["result_mode"], "geometry_preview")
                    self.assertFalse(snap_sub_high["is_computed_result"])
                    self.assertFalse(snap_sub_high["power_ledger"]["available"])
                    self.assertEqual(snap_sub_high["power_ledger"]["reason"], "CONFIGURATION_NOT_IN_RUN")
                    self.assertEqual(snap_sub_high["source"]["configuration_id"], "high")
                    self.assertEqual(snap_sub_high["source"]["recorded_configurations"], ["low"])
                    self.assertIn("CONFIGURATION_NOT_IN_RUN", {n["code"] for n in snap_sub_high["notices"]})
                    sub_hi_part = next(p for p in snap_sub_high["scene"]["parts"] if p["id"] == "signal")
                    sub_hi_qty = next(q for q in snap_sub_high["quantities"] if q["observation_id"] == "signal")
                    self.assertAlmostEqual(sub_hi_part["position_mm"][0], 120.0, places=4)
                    self.assertAlmostEqual(sub_hi_qty["plane"]["origin_mm"][0], 120.0, places=4)
                    sub_hi_targets = {t["target_id"] for t in snap_sub_high["targets"]}
                    self.assertIn("reference_power", sub_hi_targets)
                    self.assertNotIn("signal_power", sub_hi_targets)

            # 5) Renamed/removed watched configuration: NEVER silently renders first config under old name!
            with self.assertRaises(SnapshotError) as ctx_missing:
                build_snapshot(resolve_target(project_path, run=root_run, configuration="nonexistent"), revision=1)
            self.assertEqual(ctx_missing.exception.code, "CONFIGURATION_NOT_FOUND")

            once_missing = subprocess.run(
                [sys.executable, *KIT, "view", str(project_path), "--configuration", "nonexistent", "--once"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(once_missing.returncode, 4)
            self.assertEqual(json.loads(once_missing.stderr)["error"]["code"], "CONFIGURATION_NOT_FOUND")

            # Live session watching --configuration high when 'high' is removed from Project transitions to error!
            with ViewerSession(resolve_target(project_path, run=root_run, configuration="high"), poll_interval=0.08) as sess:
                snap_init = sess.load_initial().snapshot
                self.assertEqual(snap_init["source"]["configuration_id"], "high")
                self.assertEqual(snap_init["result_mode"], "computed_run")

                low_only_doc = _multiconfig_project(include_high=False)
                _write_json(project_path, low_only_doc)
                self.assertTrue(sess.poll_once())
                env_err = sess.state.envelope()
                self.assertEqual(env_err["status"], "error")
                self.assertIsNone(env_err["snapshot"])
                self.assertEqual(env_err["error"]["code"], "CONFIGURATION_NOT_FOUND")
                self.assertEqual(env_err["last_valid_snapshot"]["source"]["configuration_id"], "high")

                # Restoring 'high' recovers cleanly!
                _write_json(project_path, doc)
                self.assertTrue(sess.poll_once())
                env_ok = sess.state.envelope()
                self.assertEqual(env_ok["status"], "ok")
                self.assertEqual(env_ok["snapshot"]["source"]["configuration_id"], "high")

            # 6) Leftover user note in old configurations/high/note.txt after removing 'high' and writing
            #    a valid low-only run: manifest-only discovery opens/follows 'low', ignores 'high',
            #    and leaves note.txt completely untouched!
            note_path = root_run / "configurations" / "high" / "note.txt"
            note_bytes = b"user scratch note -- must stay untouched\n"
            note_path.write_bytes(note_bytes)
            note_hash_before = _sha256_bytes(note_path)

            low_only_doc = _multiconfig_project(include_high=False)
            _write_json(project_path, low_only_doc)
            _run_configured_project(low_only_doc, root_run)

            # Confirm leftover directory still exists on disk alongside the valid low-only run.
            self.assertTrue(note_path.is_file())
            self.assertEqual(_sha256_bytes(note_path), note_hash_before)

            for spec in (
                resolve_target(root_run),
                resolve_target(project_path, run=root_run),
                resolve_target(root_run, project=project_path),
            ):
                snap_low_only = build_snapshot(spec, revision=1).snapshot
                self.assertEqual(snap_low_only["result_mode"], "computed_run")
                self.assertTrue(snap_low_only["is_computed_result"])
                self.assertEqual(snap_low_only["source"]["configuration_id"], "low")
                self.assertEqual(snap_low_only["source"]["available_configurations"], ["low"])
                self.assertAlmostEqual(snap_low_only["power_ledger"]["total_input_W"], 0.001, places=12)
                if spec.project_path is not None:
                    self.assertEqual(snap_low_only["freshness"]["state"], "CURRENT")

            self.assertEqual(_sha256_bytes(note_path), note_hash_before)

    def test_beam_expander_offline_schematic_fallback_and_placeholder_classification(self):
        """Offline (empty catalog) beam-expander preview falls back honestly with L1/L2 schematic,
        SRC preserved as VIEW_PLACEHOLDER, beams empty, and input untouched.
        """
        before_sha = _sha256_bytes(BEAM_EXPANDER_PROJECT)
        with TempDir() as empty_catalog:
            old_dir = os.environ.get("OPENSCI_CATALOG_DIR")
            old_interval = os.environ.get("OPENSCI_CATALOG_CHECK_INTERVAL")
            os.environ["OPENSCI_CATALOG_DIR"] = str(empty_catalog)
            os.environ["OPENSCI_CATALOG_CHECK_INTERVAL"] = "0"
            try:
                built = build_snapshot(resolve_target(BEAM_EXPANDER_PROJECT), revision=1)
            finally:
                if old_dir is None:
                    os.environ.pop("OPENSCI_CATALOG_DIR", None)
                else:
                    os.environ["OPENSCI_CATALOG_DIR"] = old_dir
                if old_interval is None:
                    os.environ.pop("OPENSCI_CATALOG_CHECK_INTERVAL", None)
                else:
                    os.environ["OPENSCI_CATALOG_CHECK_INTERVAL"] = old_interval

        self.assertEqual(_sha256_bytes(BEAM_EXPANDER_PROJECT), before_sha)
        snap = built.snapshot
        self.assertEqual(snap["result_mode"], "geometry_preview")
        self.assertFalse(snap["is_computed_result"])
        self.assertTrue(snap["scene"]["geometry_only"])
        self.assertEqual(snap["scene"]["beam_count"], 0)
        self.assertEqual(built.scene_document["beams"], [])
        self.assertTrue(snap["scene"]["has_schematic_geometry"])
        self.assertIn("SCHEMATIC_GEOMETRY_USED", {n["code"] for n in snap["notices"]})

        parts = {p["id"]: p for p in snap["scene"]["parts"]}
        for lens_id in ("L1", "L2"):
            self.assertTrue(parts[lens_id]["schematic"], lens_id)
            self.assertEqual(parts[lens_id]["geometry_source"], "schematic", lens_id)
            self.assertIn(lens_id, snap["scene"]["schematic_part_ids"])
        self.assertEqual(parts["SRC"]["geometry_source"], "view_placeholder")
        self.assertTrue(parts["SRC"]["view_placeholder"])
        self.assertEqual(parts["SRC"]["geometry_basis"], "VIEW_PLACEHOLDER")
        self.assertFalse(parts["SRC"]["schematic"])

        # 2) Valid Project with an unspecified custom detector (ELEMENT_MARKER placeholder) -> marked schematic!
        with TempDir() as tmp:
            doc_custom = _detector_base()
            doc_custom["components"].append({
                "instance_id": "CUSTOM_DET",
                "optical_role": "detector",
                "custom_part": {"custom_part_key": "custom-det", "description": "Custom detector housing"},
                "nominal_pose": {"position_mm": [220.0, 0.0, 0.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]},
                "selected_parameters": [],
                "retained_dofs": [],
            })
            doc_custom["observations"].append({
                "observation_id": "custom_face",
                "device_instance_id": "CUSTOM_DET",
                "pose": {"position_mm": [0.0, 0.0, 0.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]},
                "aperture": {"shape": "circle", "radius_mm": 5.0, "center_offset_mm": [0.0, 0.0], "obstruction_radius_mm": 0.0},
                "absorbing": True,
                "medium": "air",
            })
            p_custom = _write_json(tmp / "custom.json", doc_custom)
            snap_custom = build_snapshot(resolve_target(p_custom), revision=1).snapshot
            c_parts = {p["id"]: p for p in snap_custom["scene"]["parts"]}
            self.assertTrue(c_parts["CUSTOM_DET"]["schematic"])
            self.assertTrue(c_parts["CUSTOM_DET"]["view_placeholder"])
            self.assertEqual(c_parts["CUSTOM_DET"]["geometry_source"], "schematic")
            self.assertEqual(c_parts["SRC"]["geometry_source"], "view_placeholder")
            self.assertTrue(c_parts["SRC"]["view_placeholder"])
            self.assertFalse(c_parts["SRC"]["schematic"])
            self.assertTrue(snap_custom["scene"]["has_schematic_geometry"])
            self.assertIn("CUSTOM_DET", snap_custom["scene"]["schematic_part_ids"])
            self.assertNotIn("SRC", snap_custom["scene"]["schematic_part_ids"])
            self.assertIn("SCHEMATIC_GEOMETRY_USED", {n["code"] for n in snap_custom["notices"]})

            # 3) Unlowerable Project (bare custom lens with no dimensions) -> fallback marks schematic explicitly!
            doc_bare = _detector_base()
            doc_bare["components"].append({
                "instance_id": "BARE_LENS",
                "optical_role": "lens",
                "custom_part": {"custom_part_key": "bare-lens", "description": "Unspecified custom lens"},
                "nominal_pose": {"position_mm": [80.0, 0.0, 0.0], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]},
                "selected_parameters": [],
                "retained_dofs": [],
            })
            p_bare = _write_json(tmp / "bare.json", doc_bare)
            snap_bare = build_snapshot(resolve_target(p_bare), revision=1).snapshot
            b_parts = {p["id"]: p for p in snap_bare["scene"]["parts"]}
            self.assertTrue(b_parts["BARE_LENS"]["schematic"])
            self.assertEqual(b_parts["BARE_LENS"]["geometry_source"], "schematic")
            self.assertFalse(b_parts["BS"]["schematic"])
            self.assertEqual(b_parts["BS"]["geometry_source"], "project_design_geometry")
            self.assertEqual(b_parts["SRC"]["geometry_source"], "view_placeholder")
            self.assertTrue(b_parts["SRC"]["view_placeholder"])
            self.assertTrue(snap_bare["scene"]["has_schematic_geometry"])
            self.assertIn("BARE_LENS", snap_bare["scene"]["schematic_part_ids"])
            self.assertIn("SCHEMATIC_GEOMETRY_USED", {n["code"] for n in snap_bare["notices"]})


def _quat_wxyz_mul(a: list[float], b: list[float]) -> list[float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ]


def _quat_wxyz_rotate(q: list[float], v: list[float]) -> list[float]:
    w, x, y, z = q
    conj = [w, -x, -y, -z]
    return _quat_wxyz_mul(_quat_wxyz_mul(q, [0.0, *v]), conj)[1:]


def _axis_angle_quat(axis: tuple[float, float, float], degrees: float) -> list[float]:
    import math

    half = math.radians(degrees) / 2.0
    norm = math.sqrt(sum(c * c for c in axis))
    return [math.cos(half), *(math.sin(half) * c / norm for c in axis)]


def _attached_plane_project(det_t_position_mm: list[float]) -> dict:
    """The detector-measurements case (no catalog parts) with its ``signal`` plane attached to a detector ``DET_T``:
    offset and turned in the detector's frame, and a configuration that translates AND rotates the detector. The
    ``reference`` plane stays unattached (design frame) as the control.
    """
    doc = _detector_base()
    signal = next(o for o in doc["observations"] if o["observation_id"] == "signal")
    det_pose = json.loads(json.dumps(signal["pose"]))
    doc["components"].append({
        "instance_id": "DET_T",
        "optical_role": "detector",
        "custom_part": {"custom_part_key": "user-detector", "description": "User-supplied detector."},
        "nominal_pose": det_pose,
        "selected_parameters": [],
        "retained_dofs": [],
    })
    signal["device_instance_id"] = "DET_T"
    signal["pose"] = {"position_mm": [1.5, -2.0, 0.75], "quaternion_wxyz": _axis_angle_quat((0.0, 0.0, 1.0), 25.0)}
    moved = {
        "position_mm": list(det_t_position_mm),
        "quaternion_wxyz": _quat_wxyz_mul(_axis_angle_quat((1.0, 0.0, 0.0), 15.0), det_pose["quaternion_wxyz"]),
    }
    doc["configurations"] = [{
        "configuration_id": "moved",
        "label": "Detector moved and turned",
        "component_poses": [{"instance_id": "DET_T", "nominal_pose": moved}],
    }]
    return doc


def _scene_plane_geometry(scene_document: dict, part_id: str) -> tuple[list[float], list[tuple[float, ...]]]:
    """Where a plane is actually drawn, from the view document's base64 float32 vertex positions: the bounding-box centre
    of its vertices (a disc or thin slab centred on the plane origin) and the vertices themselves."""
    import base64
    import struct

    part = next(p for p in scene_document["parts"] if p["id"] == part_id)
    points = []
    for mesh in part["meshes"]:
        raw = base64.b64decode(mesh["positions"])
        values = struct.unpack(f"<{len(raw) // 4}f", raw)
        points.extend(values[i:i + 3] for i in range(0, len(values), 3))
    centre = [(min(p[k] for p in points) + max(p[k] for p in points)) / 2.0 for k in range(3)]
    return centre, points


@requires_engine
class ViewerObservationPlaneFrameTests(unittest.TestCase):
    """Every mode reports an observation plane where the engine resolves it and the 3D scene draws it: the design frame,
    after the selected configuration, an attached plane composed with its device's pose."""

    @classmethod
    def setUpClass(cls):
        quiet_environment()

    def _expected_frames(self, det_t_position_mm: list[float]) -> dict[str, dict[str, list[float]]]:
        """Reference poses computed here, independently of the viewer: device pose composed with the local plane pose."""
        doc = _attached_plane_project(det_t_position_mm)
        moved = doc["configurations"][0]["component_poses"][0]["nominal_pose"]
        frames = {}
        for obs in doc["observations"]:
            local_q = obs["pose"]["quaternion_wxyz"]
            local_p = obs["pose"]["position_mm"]
            if obs["observation_id"] == "signal":
                q = _quat_wxyz_mul(moved["quaternion_wxyz"], local_q)
                offset = _quat_wxyz_rotate(moved["quaternion_wxyz"], local_p)
                origin = [moved["position_mm"][k] + offset[k] for k in range(3)]
            elif obs["observation_id"] == "reference":
                q, origin = local_q, list(local_p)
            else:
                continue
            frames[obs["observation_id"]] = {
                "origin_mm": origin,
                "u_axis": _quat_wxyz_rotate(q, [1.0, 0.0, 0.0]),
                "v_axis": _quat_wxyz_rotate(q, [0.0, 1.0, 0.0]),
                "normal": _quat_wxyz_rotate(q, [0.0, 0.0, 1.0]),
            }
        return frames

    def _assert_vec(self, actual, expected, label: str, places: int = 6) -> None:
        self.assertIsNotNone(actual, label)
        for k in range(3):
            self.assertAlmostEqual(float(actual[k]), float(expected[k]), places=places, msg=f"{label}: {actual} != {expected}")

    def _assert_planes(self, snap: dict, scene_document: dict, expected: dict, label: str) -> None:
        planes = {q["observation_id"]: q["plane"] for q in snap["quantities"]}
        for obs_id, frame in expected.items():
            plane = planes[obs_id]
            for key in ("origin_mm", "u_axis", "v_axis", "normal"):
                self._assert_vec(plane[key], frame[key], f"{label} {obs_id} snapshot {key}")
            centre, points = _scene_plane_geometry(scene_document, obs_id)
            self._assert_vec(centre, frame["origin_mm"], f"{label} {obs_id} drawn centre", places=3)
            # Every drawn vertex lies in the expected plane (within a thin slab's half thickness).
            off_plane = max(abs(sum((p[k] - frame["origin_mm"][k]) * frame["normal"][k] for k in range(3))) for p in points)
            self.assertLess(off_plane, 0.25, f"{label} {obs_id} drawn outside the expected plane")
        det = planes["signal"]
        self.assertEqual(det["pose_frame"], "DEVICE_LOCAL", label)
        self.assertEqual(det["device_id"], "DET_T", label)
        ctrl = planes["reference"]
        self.assertEqual(ctrl["pose_frame"], "DESIGN", label)
        self.assertIsNone(ctrl["device_id"], label)

    def _assert_withheld(self, snap: dict, label: str) -> None:
        self.assertFalse(snap["is_computed_result"], label)
        self.assertEqual(snap["scene"]["beam_count"], 0, label)
        self.assertEqual(snap["scene"]["drawable_ray_count"], 0, label)
        self.assertFalse(snap["power_ledger"]["available"], label)
        for q in snap["quantities"]:
            self.assertIsNone(q["total_power_W"], label)
            self.assertEqual(q["arrivals"], [], label)
            self.assertEqual(q["quantity_state"], "UNCOMPUTED", label)
            # The local pose stays available, labelled as what the Project wrote, in pose_frame coordinates.
            self.assertIn("declared_pose", q["plane"], label)
        det = next(q for q in snap["quantities"] if q["observation_id"] == "signal")
        self.assertEqual(det["plane"]["declared_pose"]["position_mm"], [1.5, -2.0, 0.75], label)

    def test_attached_plane_reports_resolved_design_pose_in_every_mode(self):
        from os_optics.local import json_api
        from os_optics.local.configurations import run_configured_project
        from os_optics.results import load_run
        from os_optics_kit.viewer.snapshot import _apply_configuration

        with TempDir() as tmp:
            project = tmp / "project.json"
            first, second = [120.0, 4.0, 47.0], [130.0, -6.0, 52.5]
            project.write_text(json.dumps(_attached_plane_project(first)), encoding="utf-8")
            expected_first = self._expected_frames(first)

            # The engine's own resolution of the selected configuration agrees with the reference composed here.
            applied = _apply_configuration(json.loads(project.read_text(encoding="utf-8")), "moved")
            layout_planes = {p["observation_id"]: p for p in json_api.project_layout(applied)["layout"]["observation_planes"]}
            for obs_id, frame in expected_first.items():
                self._assert_vec(layout_planes[obs_id]["pose"]["position_mm"], frame["origin_mm"], f"engine layout {obs_id}")

            # 1) Project only: geometry preview, in process, over HTTP and from the --once CLI.
            spec = resolve_target(project, configuration="moved")
            built = build_snapshot(spec)
            self.assertEqual(built.snapshot["result_mode"], "geometry_preview")
            self._assert_planes(built.snapshot, built.scene_document, expected_first, "project-only")
            self._assert_withheld(built.snapshot, "project-only")
            with ViewerSession(spec, poll_interval=5.0) as session:
                session.load_initial()
                with LocalViewerServer(session, host="127.0.0.1", port=0) as server:
                    _st, envelope, _h = _http_get_status_json(f"{server.url}api/snapshot")
                    snap_id = envelope["snapshot"]["snapshot_id"]
                    _st, view_doc, _h = _http_get_status_json(f"{server.url}engine-view/{snap_id}/view.json")
                    self._assert_planes(envelope["snapshot"], view_doc, expected_first, "project-only HTTP")
            done = subprocess.run([sys.executable, *KIT, "view", str(project), "--configuration", "moved", "--once"],
                                  capture_output=True, text=True, timeout=180)
            self.assertEqual(done.returncode, 0, done.stderr)
            once = json.loads(done.stdout)["snapshot"]
            planes_once = {q["observation_id"]: q["plane"] for q in once["quantities"]}
            for obs_id, frame in expected_first.items():
                self._assert_vec(planes_once[obs_id]["origin_mm"], frame["origin_mm"], f"--once {obs_id}")

            # 2) Matching run: the engine's traced planes, the scene and the snapshot all agree with the reference.
            run_root = tmp / "run"
            run_configured_project(json.loads(project.read_text(encoding="utf-8")), output_dir=run_root)
            computed = build_snapshot(resolve_target(project, run=run_root, configuration="moved"))
            self.assertEqual(computed.snapshot["result_mode"], "computed_run")
            run_dir = Path(computed.snapshot["source"]["run_dir"])
            traced = {m["observation_id"]: m["plane"] for m in load_run(run_dir, verify=True).monitors()}
            for obs_id, frame in expected_first.items():
                for key in ("origin_mm", "u_axis", "v_axis", "normal"):
                    self._assert_vec(traced[obs_id][key], frame[key], f"engine traced {obs_id} {key}")
            computed_planes = {q["observation_id"]: q["plane"] for q in computed.snapshot["quantities"]}
            for obs_id, frame in expected_first.items():
                self._assert_vec(computed_planes[obs_id]["origin_mm"], frame["origin_mm"], f"matching-run {obs_id}")
                self.assertEqual(computed_planes[obs_id]["pose_frame"], traced[obs_id]["pose_frame"])

            # 3) Historical: the Project moves the detector again without a rerun.
            project.write_text(json.dumps(_attached_plane_project(second)), encoding="utf-8")
            stale = build_snapshot(resolve_target(project, run=run_root, configuration="moved"))
            self.assertEqual(stale.snapshot["result_mode"], "stale_run")
            expected_second = self._expected_frames(second)
            self._assert_planes(stale.snapshot, stale.scene_document, expected_second, "historical")
            self._assert_withheld(stale.snapshot, "historical")

    def test_offset_and_rectangular_active_areas_drawn_where_the_engine_draws_them(self):
        """A plane's active area is centred at ``center_offset_mm`` in the plane; the fallback builder (no engine layout)
        draws a circular and a rectangular active area at the same place and size as the engine, as declared geometry."""
        from unittest import mock

        from os_optics.local import json_api

        position = [120.0, 4.0, 47.0]
        expected = self._expected_frames(position)["signal"]
        for shape, aperture in (
            ("circle", {"shape": "circle", "radius_mm": 2.5, "center_offset_mm": [1.0, -0.5], "obstruction_radius_mm": 0.0}),
            ("rectangle", {"shape": "rectangle", "half_width_mm": 3.0, "half_height_mm": 1.5, "center_offset_mm": [-0.8, 0.6]}),
        ):
            with self.subTest(shape=shape), TempDir() as tmp:
                doc = _attached_plane_project(position)
                next(o for o in doc["observations"] if o["observation_id"] == "signal")["aperture"] = aperture
                project = tmp / "project.json"
                project.write_text(json.dumps(doc), encoding="utf-8")
                engine = build_snapshot(resolve_target(project, configuration="moved"))
                with mock.patch.object(json_api, "project_layout", side_effect=RuntimeError("layout unavailable")):
                    fallback = build_snapshot(resolve_target(project, configuration="moved"))
                ou, ov = aperture["center_offset_mm"]
                area_centre = [expected["origin_mm"][k] + ou * expected["u_axis"][k] + ov * expected["v_axis"][k]
                               for k in range(3)]
                for label, built in (("engine", engine), ("fallback", fallback)):
                    centre, points = _scene_plane_geometry(built.scene_document, "signal")
                    self._assert_vec(centre, area_centre, f"{shape} {label} drawn active-area centre", places=3)
                    extent_u = max(abs(sum((p[k] - area_centre[k]) * expected["u_axis"][k] for k in range(3))) for p in points)
                    want_u = aperture.get("radius_mm", aperture.get("half_width_mm"))
                    self.assertAlmostEqual(extent_u, want_u, places=3, msg=f"{shape} {label} drawn half extent along u")
                    plane = next(q["plane"] for q in built.snapshot["quantities"] if q["observation_id"] == "signal")
                    self._assert_vec(plane["origin_mm"], expected["origin_mm"], f"{shape} {label} plane origin")
                self.assertNotIn("signal", fallback.snapshot["scene"]["schematic_part_ids"], shape)

    def test_layout_fallback_uses_the_same_resolved_pose(self):
        """Where the engine cannot lower the Project's layout (e.g. catalog geometry unavailable), the fallback builder
        composes the same pose; a plane attached to an undeclared device has none and is labelled schematic."""
        from unittest import mock

        from os_optics.local import json_api

        with TempDir() as tmp:
            project = tmp / "project.json"
            position = [120.0, 4.0, 47.0]
            project.write_text(json.dumps(_attached_plane_project(position)), encoding="utf-8")
            with mock.patch.object(json_api, "project_layout", side_effect=RuntimeError("layout unavailable")):
                built = build_snapshot(resolve_target(project, configuration="moved"))
            self._assert_planes(built.snapshot, built.scene_document, self._expected_frames(position), "fallback")
            self._assert_withheld(built.snapshot, "fallback")

            dangling = _attached_plane_project(position)
            next(o for o in dangling["observations"] if o["observation_id"] == "signal")[
                "device_instance_id"] = "NO_SUCH_DEVICE"
            project.write_text(json.dumps(dangling), encoding="utf-8")
            built = build_snapshot(resolve_target(project, configuration="moved"))
            row = next(q for q in built.snapshot["quantities"] if q["observation_id"] == "signal")
            self.assertIsNone(row["plane"]["origin_mm"])
            self.assertEqual(row["plane"]["pose_frame"], "DEVICE_LOCAL")
            self.assertEqual(row["plane"]["device_id"], "NO_SUCH_DEVICE")
            self.assertIn("signal", built.snapshot["scene"]["schematic_part_ids"])
            ctrl = next(q for q in built.snapshot["quantities"] if q["observation_id"] == "reference")
            self._assert_vec(ctrl["plane"]["origin_mm"], [0.0, 100.0, 50.0], "dangling control")


@unittest.skipUnless(catalog_available(), "no catalog release is installed")
@requires_engine
class ViewerCatalogGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet_environment()

    def test_beam_expander_pinned_catalog_geometry_and_source_view_placeholder(self):
        before_sha = _sha256_bytes(BEAM_EXPANDER_PROJECT)
        built = build_snapshot(resolve_target(BEAM_EXPANDER_PROJECT), revision=1)
        self.assertEqual(_sha256_bytes(BEAM_EXPANDER_PROJECT), before_sha)

        snap = built.snapshot
        self.assertEqual(snap["result_mode"], "geometry_preview")
        self.assertFalse(snap["is_computed_result"])
        self.assertTrue(snap["scene"]["geometry_only"])
        self.assertEqual(snap["scene"]["beam_count"], 0)
        self.assertEqual(built.scene_document["beams"], [])
        self.assertFalse(snap["scene"]["has_schematic_geometry"])
        self.assertNotIn("SCHEMATIC_GEOMETRY_USED", {n["code"] for n in snap["notices"]})
        self.assertEqual(snap["identity"]["scene_content_hash"], built.scene_document["identity"]["scene_content_hash"])

        parts = {p["id"]: p for p in snap["scene"]["parts"]}
        for lens_id, expected_thk in (("L1", 2.6), ("L2", 2.2)):
            lens_part = parts[lens_id]
            self.assertEqual(lens_part["geometry_source"], "catalog", lens_id)
            self.assertFalse(lens_part["schematic"], lens_id)
            self.assertFalse(lens_part["view_placeholder"], lens_id)
            self.assertAlmostEqual(lens_part["diameter_mm"], 12.7, places=3)
            self.assertAlmostEqual(lens_part["thickness_mm"], expected_thk, places=3)
            lo = lens_part["bounds"]["min_mm"]
            hi = lens_part["bounds"]["max_mm"]
            self.assertAlmostEqual(hi[0] - lo[0], expected_thk, places=2)
            self.assertAlmostEqual(hi[1] - lo[1], 12.7, places=2)
            self.assertAlmostEqual(hi[2] - lo[2], 12.7, places=2)

        # SRC is an engine VIEW_PLACEHOLDER emitter marker (10 mm), NOT exact physical laser housing.
        src_part = parts["SRC"]
        self.assertEqual(src_part["geometry_source"], "view_placeholder")
        self.assertTrue(src_part["view_placeholder"])
        self.assertEqual(src_part["geometry_basis"], "VIEW_PLACEHOLDER")
        self.assertEqual(src_part["geometry_status"], "PLACEHOLDER")
        self.assertEqual(src_part["geometry_kind"], "SOURCE_EMITTER")
        self.assertFalse(src_part["schematic"])
        self.assertIn("SRC", snap["scene"]["view_placeholder_part_ids"])
        self.assertIn("catalog", snap["scene"]["geometry_sources"])
        self.assertIn("view_placeholder", snap["scene"]["geometry_sources"])


if __name__ == "__main__":
    unittest.main()
