"""Local read-only live viewer for native OS Optics Projects and saved run directories.

Usage:
  os-optics-kit view PATH [--host 127.0.0.1] [--port 0]
                          [--project PROJECT.json] [--run RUN_DIR]
                          [--configuration ID] [--poll-interval 0.4]
                          [--no-open] [--evaluate] [--once]

PATH is a native Project JSON file or a saved run directory written by ``os-optics project run``.
The viewer is strictly read-only: it binds loopback only, serves GET/HEAD only, and never modifies
input files.

A Project file with no ``--run`` shows a nominal geometry preview (never presented as a computed
optical result). Pass ``--run RUN_DIR`` to view a saved run alongside its Project with live
freshness checking, or pass ``--evaluate`` to perform one explicit local evaluation into a
temporary directory (without rewriting the input Project and without re-running on file edits).

When serving, prints one compact JSON startup record on stdout, opens the default browser unless
``--no-open`` is set, live-follows updates over Server-Sent Events, and stops cleanly on Ctrl-C /
SIGTERM. Pass ``--once`` to print one snapshot envelope JSON on stdout and exit without serving.

Exit codes: 0 clean stop or snapshot printed, 4 invalid input, refused host, or missing engine.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import signal
import sys
import threading
from typing import Any

from ._engine import require_engine
from .interop import json_safe
from .result_summary import ArgumentParser, ERROR_EXIT, error_document, exit_status, json_cli_stdout

PROG = "os-optics-kit view"


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog=PROG,
        description=__doc__.split("\n")[0] if __doc__ else "",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", type=Path, help="Native Project JSON file or saved local run directory")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (127.0.0.1, localhost, ::1; default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="Loopback TCP port (default: 0 for an ephemeral free port)")
    parser.add_argument("--project", type=Path, default=None, help="Optional Project JSON file for freshness tracking when PATH is a run directory")
    parser.add_argument("--run", type=Path, default=None, help="Optional saved run directory when PATH is a Project JSON file")
    parser.add_argument("--configuration", default=None, help="Configuration ID when viewing a multi-configuration run directory")
    parser.add_argument("--poll-interval", type=float, default=0.4, help="Live-follow poll interval in seconds (default: 0.4)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default web browser automatically")
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Explicit one-shot local evaluation for a Project JSON with no --run (writes to a temporary directory; never modifies input or auto-reruns on edits)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Build one snapshot envelope, print compact JSON to stdout, and exit without starting an HTTP server",
    )
    return parser


def _print_json_stdout(payload: Any) -> None:
    print(json.dumps(json_safe(payload), ensure_ascii=True, allow_nan=False, separators=(",", ":")), flush=True)


def _print_json_error(exc: Exception) -> None:
    error = exc.as_dict() if hasattr(exc, "as_dict") and callable(exc.as_dict) else error_document(exc)
    print(json.dumps({"error": json_safe(error)}, ensure_ascii=True, separators=(",", ":")), file=sys.stderr, flush=True)


def _wait_until_stopped(stop_event: threading.Event) -> None:
    previous_sigint = None
    previous_sigterm = None
    can_install = threading.current_thread() is threading.main_thread()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    if can_install:
        previous_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_signal)
        if hasattr(signal, "SIGTERM"):
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_signal)
    try:
        while not stop_event.wait(0.25):
            continue
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        if can_install:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
            if previous_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    try:
        with json_cli_stdout():
            args = build_parser().parse_args(argv)
            from .viewer.server import LocalViewerServer, validate_loopback_host
            from .viewer.snapshot import SnapshotError, resolve_target
            from .viewer.watcher import ViewerSession

            # Validate engine-independent CLI arguments and input paths BEFORE requiring os-optics.
            host = validate_loopback_host(args.host)
            if not (0 <= args.port <= 65535):
                raise SnapshotError("USAGE", f"--port must be an integer between 0 and 65535, got {args.port!r}")
            if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
                raise SnapshotError("USAGE", "--poll-interval must be a positive number of seconds")
            spec = resolve_target(
                args.path,
                project=args.project,
                run=args.run,
                configuration=args.configuration,
            )
            if args.evaluate:
                if spec.kind != "project":
                    raise SnapshotError("USAGE", "--evaluate can only be used with a Project JSON file (not a run directory)")
                if spec.run_dir is not None:
                    raise SnapshotError("USAGE", "--evaluate cannot be combined with --run")

            require_engine()
            session = ViewerSession(
                spec,
                poll_interval=args.poll_interval,
                evaluate=args.evaluate,
            )
            try:
                session.load_initial()
                if args.once:
                    envelope = session.state.envelope({"read_only": True, "once": True})
                    session.close()
                    payload = envelope
                else:
                    session.start()
                    server = LocalViewerServer(session, host=host, port=args.port)
                    payload = server.startup_info()
            except Exception:
                session.close()
                raise
    except SystemExit as exc:
        return exit_status(exc)
    except Exception as exc:  # noqa: BLE001
        _print_json_error(exc)
        return ERROR_EXIT

    if args.once:
        _print_json_stdout(payload)
        return 0

    try:
        _print_json_stdout(payload)
        if not args.no_open:
            server.open_browser()
        _wait_until_stopped(threading.Event())
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
