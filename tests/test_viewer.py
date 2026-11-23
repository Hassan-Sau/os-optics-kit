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
from os_optics_kit.viewer import LocalViewerServer, ViewerSession, build_snapshot, resolve_target
from tests.workflow_fixtures import KIT, ROOT, TempDir, quiet_environment, requires_engine

DETECTOR_CASE_PROJECT = ROOT / "cases" / "detector-measurements" / "project.json"


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


def _run_project(doc: dict, out_dir: Path) -> Path:
    from os_optics.local import run_optical_project

    run_optical_project(doc, output_dir=out_dir)
    return out_dir


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
        block = (
            "import sys\n"
            "class B:\n"
            "  def find_spec(self, fullname, *a):\n"
            "    if fullname == 'os_optics' or fullname.startswith('os_optics.'):\n"
            "      raise ModuleNotFoundError(fullname, name=fullname)\n"
            "sys.meta_path.insert(0, B())\n"
            "from os_optics_kit.cli import main\n"
            "raise SystemExit(main(['view', '--help']))\n"
        )
        done = subprocess.run([sys.executable, "-c", block], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("usage: os-optics-kit view"))

    def test_non_loopback_host_refused(self):
        with TempDir() as tmp:
            p = _write_json(tmp / "p.json", _detector_base())
            done = subprocess.run(
                [sys.executable, *KIT, "view", str(p), "--host", "0.0.0.0", "--no-open"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 4)
            err = json.loads(done.stderr)["error"]
            self.assertEqual(err["code"], "HOST_NOT_LOOPBACK")

    def test_missing_path_refused(self):
        with TempDir() as tmp:
            done = subprocess.run(
                [sys.executable, *KIT, "view", str(tmp / "missing.json"), "--once"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 4)
            err = json.loads(done.stderr)["error"]
            self.assertEqual(err["code"], "INPUT_NOT_FOUND")


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


if __name__ == "__main__":
    unittest.main()
