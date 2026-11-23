"""Read-only local live viewer for native OS Optics Projects and saved run directories."""

from __future__ import annotations

from .server import LocalViewerServer
from .snapshot import (
    SNAPSHOT_SCHEMA,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotError,
    SnapshotTransientError,
    TargetSpec,
    build_snapshot,
    resolve_target,
)
from .watcher import ViewerSession, ViewerState

__all__ = [
    "LocalViewerServer",
    "SNAPSHOT_SCHEMA",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotError",
    "SnapshotTransientError",
    "TargetSpec",
    "ViewerSession",
    "ViewerState",
    "build_snapshot",
    "resolve_target",
]
