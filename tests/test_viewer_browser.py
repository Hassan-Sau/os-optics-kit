"""Real-browser live-viewer regressions (Playwright + Chromium).

Verifies that in an actual browser:
1. Reordering or delaying normal HTTP reads (both ``/api/snapshot`` and ``/engine-view/<id>/view.json``)
   while a separate process replaces fixture A (1 mW, completed, 2 branches) with fixture B
   (3 mW, partial, 1 branch) NEVER produces a visible mismatch between the panel numbers and
   the active WebGL 3D scene.
2. On-disk integrity corruption (B with A's ``ray_data.json``) and truncated writes show a
   visible waiting banner while holding the last verified snapshot, and recover automatically
   to valid B without reopening or reloading the page.

Missing optional dependencies (``os_optics``, ``playwright``, or Chrome/Chromium) skip these
checks cleanly; ordinary users of ``os-optics-kit view`` never need Playwright.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from os_optics_kit import render_views as render
from os_optics_kit.viewer import LocalViewerServer, ViewerSession, resolve_target

ROOT = Path(__file__).resolve().parents[1]
DETECTOR_CASE_PROJECT = ROOT / "cases" / "detector-measurements" / "project.json"


def _fixture_a() -> dict:
    """Fixture A: 1 mW, completed, 2 traced branches."""
    doc = json.loads(DETECTOR_CASE_PROJECT.read_text(encoding="utf-8"))
    for comp in doc["components"]:
        if comp["instance_id"] == "SRC":
            comp["source_model"]["power_w"] = 0.001
    for obs in doc["observations"]:
        if obs["observation_id"] == "reference":
            obs["absorbing"] = False
        if obs["observation_id"] == "signal":
            obs["aperture"]["radius_mm"] = 0.4
    return doc


def _fixture_b() -> dict:
    """Fixture B: 3 mW, scientifically PARTIAL (max_paths=1), 1 traced branch."""
    doc = json.loads(DETECTOR_CASE_PROJECT.read_text(encoding="utf-8"))
    for comp in doc["components"]:
        if comp["instance_id"] == "SRC":
            comp["source_model"]["power_w"] = 0.003
    doc["compute_budget"] = {
        "max_paths": 1,
        "max_trace_steps": 2048,
        "max_interactions_per_path": 64,
        "max_interference_pairs": 256,
    }
    return doc


def _replace_dir_from_subprocess(src_dir: Path, dst_dir: Path) -> None:
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


def _copy_file_from_subprocess(src: Path, dst: Path) -> None:
    code = "from pathlib import Path; import shutil, sys; shutil.copy2(Path(sys.argv[1]), Path(sys.argv[2]))"
    done = subprocess.run([sys.executable, "-c", code, str(src), str(dst)], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"subprocess copy failed: {done.stderr}")


def _write_text_from_subprocess(dst: Path, text: str) -> None:
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
    done = subprocess.run([sys.executable, "-c", code, str(dst), text], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise RuntimeError(f"subprocess write failed: {done.stderr}")


def _observe_visible_state(page) -> dict:
    """Inspect BOTH the visible host panel AND the active visible WebGL 3D iframe together."""
    return page.evaluate(
        """() => {
            const app = document.getElementById('app');
            const activeFrame = document.querySelector('iframe.engine-frame.active');
            const errBanner = document.getElementById('banner-error');
            const powerText = (document.getElementById('power-body')?.textContent || '').trim();
            const sceneMeta = (document.getElementById('scene-meta')?.textContent || '').trim();
            const revText = (document.getElementById('badge-rev')?.textContent || '').trim();
            const modeText = (document.getElementById('badge-mode')?.textContent || '').trim();
            const execText = (document.getElementById('execution-kpis')?.textContent || '').trim();
            let sceneInfo = null;
            try {
                const win = activeFrame && activeFrame.contentWindow;
                const v = win && win.__opensciViewer;
                if (v && v.doc) {
                    sceneInfo = {
                        project_hash: v.doc.identity?.project_hash || null,
                        scene_content_hash: v.doc.identity?.scene_content_hash || null,
                        beam_count: Array.isArray(v.doc.beams) ? v.doc.beams.length : 0,
                        part_count: Array.isArray(v.doc.parts) ? v.doc.parts.length : 0,
                        rendered_beam_objects: Array.isArray(v.beamObjects) ? v.beamObjects.length : 0,
                        frame_snapshot_id: activeFrame.getAttribute('data-snapshot-id') || '',
                        frame_src: activeFrame.getAttribute('src') || '',
                    };
                }
            } catch (e) {
                sceneInfo = { error: String(e) };
            }
            return {
                app_revision: app?.getAttribute('data-revision') || '',
                app_snapshot_id: app?.getAttribute('data-snapshot-id') || '',
                rev_text: revText,
                mode_text: modeText,
                power_text: powerText,
                scene_meta: sceneMeta,
                exec_text: execText,
                banner_visible: !!(errBanner && !errBanner.classList.contains('hidden')),
                banner_text: (errBanner?.textContent || '').trim(),
                scene: sceneInfo,
            };
        }"""
    )


def _assert_coherent_a_or_b(obs: dict, *, hash_a: str, hash_b: str) -> None:
    """Assert that the visible panel and active 3D scene ALWAYS agree on either A or B (never mixed)."""
    scene = obs["scene"]
    assert scene is not None and "error" not in scene, f"active 3D scene unavailable: {obs}"
    assert obs["app_snapshot_id"] == scene["frame_snapshot_id"], f"panel/iframe snapshot mismatch: {obs}"

    if scene["project_hash"] == hash_a:
        assert scene["beam_count"] == 2, f"A scene must have 2 beams: {obs}"
        assert "1 mW" in obs["power_text"], f"A scene shown beside non-A power panel: {obs}"
        assert "3 mW" not in obs["power_text"], f"B power leaked onto A scene: {obs}"
        assert "2 branches" in obs["scene_meta"], f"A scene meta mismatch: {obs}"
    elif scene["project_hash"] == hash_b:
        assert scene["beam_count"] == 1, f"B scene must have 1 beam: {obs}"
        assert "3 mW" in obs["power_text"], f"B scene shown beside non-B power panel: {obs}"
        assert "1 mW" not in obs["power_text"], f"A power leaked onto B scene: {obs}"
        assert "1 branches" in obs["scene_meta"], f"B scene meta mismatch: {obs}"
    else:
        raise AssertionError(f"unexpected project_hash in visible scene: {obs}")


@pytest.fixture(scope="module")
def browser_env(tmp_path_factory):
    pytest.importorskip("os_optics")
    pytest.importorskip("playwright.sync_api")
    browser_path = render.find_browser(None)
    if not browser_path:
        pytest.skip("Chrome/Chromium is not installed")

    from os_optics.local import run_optical_project
    from os_optics.results import load_run

    base = tmp_path_factory.mktemp("viewer-browser")
    run_a = base / "run_a"
    run_b = base / "run_b"
    run_optical_project(_fixture_a(), output_dir=run_a)
    run_optical_project(_fixture_b(), output_dir=run_b)
    hash_a = load_run(run_a, verify=True).identity()["project_hash"]
    hash_b = load_run(run_b, verify=True).identity()["project_hash"]
    assert hash_a != hash_b
    return browser_path, run_a, run_b, hash_a, hash_b


def test_real_browser_delayed_reads_and_integrity_recovery_never_mix_runs(browser_env, tmp_path):
    from playwright.sync_api import sync_playwright

    browser_path, run_a, run_b, hash_a, hash_b = browser_env
    watched = tmp_path / "watched_run"
    shutil.copytree(run_a, watched)

    with ViewerSession(resolve_target(watched), poll_interval=0.08) as session:
        session.load_initial()
        session.start()
        with LocalViewerServer(session, host="127.0.0.1", port=0, heartbeat_interval=2.0) as server:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 840})
                    console_errors: list[str] = []
                    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

                    # Delay view.json for rev-2 so B's scene arrives slower than /api/snapshot,
                    # AND test that the visible panel + active 3D scene stay strictly atomic throughout!
                    delay_events: list[str] = []
                    delay_lock = threading.Lock()

                    def route_view_json(route):
                        url = route.request.url
                        if "rev-2-" in url:
                            with delay_lock:
                                delay_events.append(url)
                            time.sleep(0.45)
                        route.continue_()

                    page.route("**/engine-view/*/view.json", route_view_json)

                    page.goto(f"{server.url}?lang=en", wait_until="domcontentloaded")
                    page.wait_for_function(
                        "document.getElementById('app')?.getAttribute('data-revision') === '1'",
                        timeout=20000,
                    )
                    obs_a = _observe_visible_state(page)
                    _assert_coherent_a_or_b(obs_a, hash_a=hash_a, hash_b=hash_b)
                    assert obs_a["scene"]["project_hash"] == hash_a

                    # 1) Separate process replaces A -> B while rev-2 view.json is delayed in flight.
                    _replace_dir_from_subprocess(run_b, watched)
                    deadline = time.monotonic() + 20.0
                    reached_b = False
                    while time.monotonic() < deadline:
                        obs = _observe_visible_state(page)
                        _assert_coherent_a_or_b(obs, hash_a=hash_a, hash_b=hash_b)
                        if obs["scene"]["project_hash"] == hash_b:
                            reached_b = True
                            break
                        time.sleep(0.05)
                    assert reached_b, "page never updated to coherent Run B"
                    assert delay_events, "delayed rev-2 view.json route did not trigger"

                    # 2) On-disk integrity mixing: keep B in place but copy A's ray_data.json into watched.
                    _copy_file_from_subprocess(run_a / "ray_data.json", watched / "ray_data.json")
                    page.wait_for_function(
                        "() => { const b = document.getElementById('banner-error'); return b && !b.classList.contains('hidden') && b.textContent.includes('RUN_INTEGRITY_FAILED'); }",
                        timeout=15000,
                    )
                    obs_corrupt = _observe_visible_state(page)
                    assert obs_corrupt["banner_visible"]
                    assert "RUN_INTEGRITY_FAILED" in obs_corrupt["banner_text"]
                    # Last genuinely verified snapshot (B) stays intact and coherent!
                    _assert_coherent_a_or_b(obs_corrupt, hash_a=hash_a, hash_b=hash_b)
                    assert obs_corrupt["scene"]["project_hash"] == hash_b

                    # 3) Truncated write -> stays in waiting state holding B.
                    _write_text_from_subprocess(watched / "simulation_result.json", '{"truncated": ')
                    time.sleep(0.25)
                    obs_trunc = _observe_visible_state(page)
                    assert obs_trunc["banner_visible"]
                    _assert_coherent_a_or_b(obs_trunc, hash_a=hash_a, hash_b=hash_b)
                    assert obs_trunc["scene"]["project_hash"] == hash_b

                    # 4) Separate process restores clean A -> automatic recovery without reopening page!
                    _replace_dir_from_subprocess(run_a, watched)
                    page.wait_for_function(
                        "() => { const b = document.getElementById('banner-error'); return document.getElementById('app')?.getAttribute('data-revision') !== '2' && b && b.classList.contains('hidden'); }",
                        timeout=20000,
                    )
                    obs_rec = _observe_visible_state(page)
                    assert not obs_rec["banner_visible"]
                    _assert_coherent_a_or_b(obs_rec, hash_a=hash_a, hash_b=hash_b)
                    assert obs_rec["scene"]["project_hash"] == hash_a
                    assert console_errors == [], f"unexpected browser console errors: {console_errors}"
                finally:
                    browser.close()
