"""Background live-follow watcher and thread-safe atomic state store for the local viewer."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import queue
import shutil
import tempfile
import threading
from typing import Any

from ..interop import json_safe
from .snapshot import (
    BuiltSnapshot,
    SnapshotError,
    SnapshotTransientError,
    TargetSpec,
    build_snapshot,
    compute_fingerprint,
    run_explicit_local_evaluation,
)

STATE_SCHEMA = "os-optics-kit.local_viewer.state"
STATE_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ViewerState:
    """Thread-safe container for the active snapshot, last valid snapshot, error state and SSE subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status: str = "waiting"
        self._state_seq: int = 0
        self._revision: int = 0
        self._current_snapshot: dict[str, Any] | None = None
        self._last_valid_snapshot: dict[str, Any] | None = None
        self._last_valid_scene_document: dict[str, Any] | None = None
        self._error: dict[str, Any] | None = None
        self._error_key: tuple[str, str, str | None] | None = None
        self._updated_at: str = _utc_now()
        self._closed: bool = False
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]] | None]] = set()

    def next_revision(self) -> int:
        with self._lock:
            return self._revision + 1

    def publish_snapshot(self, built: BuiltSnapshot) -> dict[str, Any]:
        with self._lock:
            self._revision = int(built.snapshot["revision"])
            self._state_seq += 1
            self._status = "ok"
            self._current_snapshot = built.snapshot
            self._last_valid_snapshot = built.snapshot
            self._last_valid_scene_document = built.scene_document
            self._error = None
            self._error_key = None
            self._updated_at = _utc_now()
            event = self._event_payload_locked()
            self._broadcast_locked("snapshot", event)
            return event

    def publish_problem(
        self,
        *,
        status: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> bool:
        """Record a waiting/error transition while preserving ``last_valid_snapshot`` intact.

        Returns True when a new transition was broadcast, False when identical to the active error.
        """
        assert status in ("waiting", "error")
        key = (status, code, fingerprint)
        with self._lock:
            if self._status == status and self._error_key == key and self._error and self._error.get("message") == message:
                return False
            self._state_seq += 1
            self._status = status
            self._current_snapshot = None
            self._updated_at = _utc_now()
            self._error = json_safe({
                "code": code,
                "message": message,
                "details": details or {},
                "at": self._updated_at,
            })
            self._error_key = key
            event = self._event_payload_locked()
            self._broadcast_locked("status", event)
            return True

    def _event_payload_locked(self) -> dict[str, Any]:
        active = self._current_snapshot or self._last_valid_snapshot
        return json_safe({
            "state_seq": self._state_seq,
            "revision": self._revision,
            "snapshot_id": active["snapshot_id"] if active is not None else None,
            "status": self._status,
            "result_mode": active["result_mode"] if active is not None else None,
            "is_computed_result": active["is_computed_result"] if active is not None else False,
            "freshness_state": (active.get("freshness") or {}).get("state") if active is not None else None,
            "error": self._error,
            "updated_at": self._updated_at,
        })

    def _broadcast_locked(self, event_name: str, payload: dict[str, Any]) -> None:
        stale: list[queue.Queue[tuple[str, dict[str, Any]] | None]] = []
        for sub in list(self._subscribers):
            try:
                sub.put_nowait((event_name, payload))
            except queue.Full:
                try:
                    sub.get_nowait()
                    sub.put_nowait((event_name, payload))
                except queue.Empty:
                    stale.append(sub)
        for sub in stale:
            self._subscribers.discard(sub)

    def event_payload(self) -> dict[str, Any]:
        with self._lock:
            return self._event_payload_locked()

    def envelope(self, server_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            return json_safe({
                "schema": STATE_SCHEMA,
                "schema_version": STATE_SCHEMA_VERSION,
                "status": self._status,
                "state_seq": self._state_seq,
                "revision": self._revision,
                "updated_at": self._updated_at,
                "snapshot": self._current_snapshot,
                "last_valid_snapshot": self._last_valid_snapshot,
                "error": self._error,
                "server": server_meta or {},
            })

    def scene_document(self) -> tuple[dict[str, Any] | None, int, str | None]:
        with self._lock:
            snap = self._last_valid_snapshot
            snap_id = snap["snapshot_id"] if snap is not None else None
            return self._last_valid_scene_document, self._revision, snap_id

    def subscribe(self) -> queue.Queue[tuple[str, dict[str, Any]] | None]:
        sub: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(maxsize=64)
        with self._lock:
            if self._closed:
                sub.put_nowait(None)
                return sub
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue[tuple[str, dict[str, Any]] | None]) -> None:
        with self._lock:
            self._subscribers.discard(sub)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for sub in list(self._subscribers):
                try:
                    sub.put_nowait(None)
                except queue.Full:
                    try:
                        sub.get_nowait()
                        sub.put_nowait(None)
                    except queue.Empty:
                        pass
            self._subscribers.clear()


class ViewerSession:
    """Owns the resolved read-only target, optional one-shot temp evaluation, state store and watcher thread."""

    def __init__(
        self,
        spec: TargetSpec,
        *,
        poll_interval: float = 0.4,
        evaluate: bool = False,
    ) -> None:
        if poll_interval <= 0:
            raise SnapshotError("USAGE", "--poll-interval must be a positive number of seconds")
        if evaluate:
            if spec.kind != "project":
                raise SnapshotError("USAGE", "--evaluate can only be used with a Project JSON file (not a run directory)")
            if spec.run_dir is not None:
                raise SnapshotError("USAGE", "--evaluate cannot be combined with --run")

        self.poll_interval = float(poll_interval)
        self.state = ViewerState()
        self._temp_eval_dir: Path | None = None
        self._spec = spec
        self._last_seen_fp: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

        if evaluate and spec.project_path is not None:
            temp_dir = Path(tempfile.mkdtemp(prefix="os-optics-kit-view-eval-")).resolve()
            self._temp_eval_dir = temp_dir
            try:
                eval_run_dir, project_hash = run_explicit_local_evaluation(spec.project_path, temp_dir)
            except Exception:
                shutil.rmtree(temp_dir, ignore_errors=True)
                self._temp_eval_dir = None
                raise
            self._spec = replace(
                spec,
                run_dir=eval_run_dir,
                evaluation_mode="explicit_local_temp",
                evaluated_project_hash=project_hash,
            )

    @property
    def spec(self) -> TargetSpec:
        return self._spec

    def load_initial(self) -> BuiltSnapshot:
        """Build and publish the initial snapshot synchronously."""
        built = build_snapshot(self._spec, revision=self.state.next_revision())
        self._last_seen_fp = built.fingerprint
        self.state.publish_snapshot(built)
        return built

    def poll_once(self) -> bool:
        """Check watched files once; publish an atomic update or waiting/error transition if changed.

        Returns True if the state transitioned, False otherwise.
        """
        try:
            fp = compute_fingerprint(self._spec)
        except SnapshotTransientError as exc:
            self._last_seen_fp = None
            return self.state.publish_problem(
                status="waiting",
                code=exc.code,
                message=str(exc),
                details=exc.details,
                fingerprint=None,
            )
        except SnapshotError as exc:
            self._last_seen_fp = None
            return self.state.publish_problem(
                status="error",
                code=exc.code,
                message=str(exc),
                details=exc.details,
                fingerprint=None,
            )

        if fp == self._last_seen_fp and self.state.event_payload()["status"] == "ok":
            return False

        self._last_seen_fp = fp
        try:
            built = build_snapshot(self._spec, revision=self.state.next_revision())
        except SnapshotTransientError as exc:
            return self.state.publish_problem(
                status="waiting",
                code=exc.code,
                message=str(exc),
                details=exc.details,
                fingerprint=fp,
            )
        except SnapshotError as exc:
            return self.state.publish_problem(
                status="error",
                code=exc.code,
                message=str(exc),
                details=exc.details,
                fingerprint=fp,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the watcher thread on unexpected IO/parse glitches
            return self.state.publish_problem(
                status="waiting",
                code="WATCHER_READ_ERROR",
                message=f"transient error while reloading snapshot: {exc}",
                details={"exception": type(exc).__name__},
                fingerprint=fp,
            )

        self._last_seen_fp = built.fingerprint
        self.state.publish_snapshot(built)
        return True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="os-optics-kit-viewer-watcher", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.poll_once()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self.state.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._temp_eval_dir is not None:
            shutil.rmtree(self._temp_eval_dir, ignore_errors=True)
            self._temp_eval_dir = None

    def __enter__(self) -> ViewerSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
