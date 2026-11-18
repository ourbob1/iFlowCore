"""
Centralized crash reporting and error logging.

`ErrorReporter` is installed once at startup (before the database unlocks, so
startup crashes are caught too) and collects errors from four places:

  * `sys.excepthook`       uncaught exceptions on the main thread
  * `threading.excepthook` uncaught exceptions in background threads
                           (radar refresh, receipt scans, motion bridge)
  * a logging handler      anything logged at ERROR or above, which includes
                           the view error boundary's `log.exception`
  * `capture_exception`    explicit calls for handled-but-notable failures

Every event is scrubbed (core.telemetry.scrub), fingerprinted (exception type
plus in-app frames) and written to two small JSONL files in `<data_dir>/logs`:

  errors.jsonl           local error log, rotated at `max_log_bytes`
  telemetry-queue.jsonl  events waiting for upload; repeats of the same
                         fingerprint bump a counter instead of adding rows,
                         so a crash loop can't fill the disk or the server

Nothing leaves the device unless the user opts in (Settings → AI service &
diagnostics). `flush(sender)` then uploads the queue to the user's own server
and removes what was accepted. Upload failures leave the queue intact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform as py_platform
import sys
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable

from core.telemetry.scrub import scrub_filename, scrub_text
from core.timeutil import to_iso_utc

log = logging.getLogger(__name__)

LOG_FILENAME = "errors.jsonl"
QUEUE_FILENAME = "telemetry-queue.jsonl"
INSTALL_ID_FILENAME = "install-id"
MAX_FRAMES = 30
UPLOAD_BATCH = 50
SCHEMA_VERSION = 1

Sender = Callable[[list[dict[str, Any]]], int]


@dataclass(frozen=True)
class ReporterInfo:
    """Static facts attached to every event. No user or device identifiers
    beyond a random install id (regenerated if the app data is wiped)."""

    release: str
    environment: str
    platform: str


class ErrorReporter:
    def __init__(self, log_dir: str | Path, info: ReporterInfo, *, clock: Callable[[], datetime] | None = None,
                 max_log_bytes: int = 512 * 1024, max_queue: int = 200,
                 extra_scrub_paths: Iterable[str | Path] = ()) -> None:
        self.log_dir = Path(log_dir)
        self.info = info
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_log_bytes = max_log_bytes
        self.max_queue = max_queue
        self._scrub_paths = tuple(str(p) for p in extra_scrub_paths)
        self._lock = threading.RLock()
        self._local = threading.local()          # re-entrancy guard (logging inside capture)
        self._installed: dict[str, Any] = {}
        self._handler: logging.Handler | None = None
        self.context: dict[str, str] = {}        # e.g. {"route": "/finances"}; scrubbed on capture
        self._install_id: str | None = None

    # -- paths ------------------------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.log_dir / LOG_FILENAME

    @property
    def queue_path(self) -> Path:
        return self.log_dir / QUEUE_FILENAME

    @property
    def install_id(self) -> str:
        if self._install_id is None:
            path = self.log_dir / INSTALL_ID_FILENAME
            try:
                value = path.read_text(encoding="ascii").strip()
                uuid.UUID(value)
            except (OSError, ValueError):
                value = str(uuid.uuid4())
                try:
                    self.log_dir.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="ascii")
                except OSError:
                    pass
            self._install_id = value
        return self._install_id

    # -- capture ----------------------------------------------------------------------

    def capture_exception(self, exc: BaseException, *, handled: bool = True, source: str = "manual",
                          tb: TracebackType | None = None) -> dict[str, Any] | None:
        """Record an exception. Returns the stored event (None if suppressed)."""
        if getattr(self._local, "busy", False):
            return None
        self._local.busy = True
        try:
            frames = self._frames(tb or exc.__traceback__)
            exc_type = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
            event = self._event(level="fatal" if not handled else "error", kind="exception", type_=exc_type,
                                message=str(exc) or exc_type, frames=frames, handled=handled, source=source)
            if exc.__cause__ is not None or exc.__context__ is not None:
                cause = exc.__cause__ or exc.__context__
                event["cause"] = f"{type(cause).__name__}: {scrub_text(cause, extra_paths=self._scrub_paths, limit=200)}"
            self._store(event)
            return event
        except Exception:                       # the reporter must never take the app down
            return None
        finally:
            self._local.busy = False

    def capture_message(self, message: str, *, level: str = "error", logger: str | None = None,
                        source: str = "manual") -> dict[str, Any] | None:
        if getattr(self._local, "busy", False):
            return None
        self._local.busy = True
        try:
            event = self._event(level=level, kind="message", type_=logger or "message", message=message,
                                frames=[], handled=True, source=source)
            self._store(event)
            return event
        except Exception:
            return None
        finally:
            self._local.busy = False

    def _frames(self, tb: TracebackType | None) -> list[dict[str, Any]]:
        frames = []
        for fs in traceback.extract_tb(tb)[-MAX_FRAMES:]:
            file, in_app = scrub_filename(fs.filename)
            frames.append({"file": file, "line": fs.lineno, "function": fs.name, "in_app": in_app})
        return frames

    def _event(self, *, level: str, kind: str, type_: str, message: str, frames: list[dict[str, Any]],
               handled: bool, source: str) -> dict[str, Any]:
        now = to_iso_utc(self._clock())
        clean = scrub_text(message, extra_paths=self._scrub_paths)
        in_app = [f"{f['file']}:{f['function']}" for f in frames if f["in_app"]] or \
                 [f"{f['file']}:{f['function']}" for f in frames[-3:]]
        # Line numbers are left out of the fingerprint so a small edit elsewhere in
        # the file doesn't split one issue into many; messages are left out because
        # they usually carry values.
        basis = "|".join([type_, *in_app]) if frames else f"{type_}|{clean}"
        return {
            "schema": SCHEMA_VERSION,
            "id": str(uuid.uuid4()),
            "timestamp": now,
            "first_seen": now,
            "last_seen": now,
            "count": 1,
            "level": level,
            "kind": kind,
            "type": type_,
            "message": clean,
            "frames": frames,
            "fingerprint": hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32],
            "handled": handled,
            "source": source,
            "context": {k: scrub_text(v, extra_paths=self._scrub_paths, limit=100) for k, v in self.context.items()},
            "release": self.info.release,
            "environment": self.info.environment,
            "platform": self.info.platform,
            "runtime": f"python {py_platform.python_version()}",
            "install_id": self.install_id,
        }

    # -- storage ----------------------------------------------------------------------

    def _store(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._append_log(event)
            queue = self._read_queue()
            for queued in queue:
                if queued.get("fingerprint") == event["fingerprint"]:
                    queued["count"] = int(queued.get("count", 1)) + 1
                    queued["last_seen"] = event["timestamp"]
                    queued["message"] = event["message"]
                    break
            else:
                queue.append(event)
            self._write_queue(queue[-self.max_queue:])

    def _append_log(self, event: dict[str, Any]) -> None:
        path = self.log_path
        try:
            if path.exists() and path.stat().st_size >= self.max_log_bytes:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")

    def _read_queue(self) -> list[dict[str, Any]]:
        try:
            lines = self.queue_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue                         # a torn write from a crash: skip the line
            if isinstance(item, dict):
                out.append(item)
        return out

    def _write_queue(self, events: list[dict[str, Any]]) -> None:
        tmp = self.queue_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.queue_path)

    # -- reading ----------------------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_queue()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first events from the local log (current file only)."""
        with self._lock:
            try:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        """Delete the local log and the upload queue (Settings → Clear)."""
        with self._lock:
            for p in (self.log_path, self.log_path.with_suffix(".jsonl.1"), self.queue_path):
                p.unlink(missing_ok=True)

    # -- upload -----------------------------------------------------------------------

    def flush(self, sender: Sender, *, batch_size: int = UPLOAD_BATCH) -> int:
        """Upload queued events in batches. `sender` returns how many it accepted
        and raises on failure; accepted events leave the queue. Returns the total
        sent. Events captured during an upload are kept."""
        sent = 0
        while True:
            with self._lock:
                batch = self._read_queue()[:batch_size]
            if not batch:
                return sent
            accepted = sender(batch)
            ids = {e["id"] for e in batch}
            with self._lock:
                remaining = [e for e in self._read_queue() if e.get("id") not in ids]
                self._write_queue(remaining)
            sent += len(batch)
            if accepted < len(batch):
                log.warning("Telemetry server accepted %d of %d events", accepted, len(batch))
            if len(batch) < batch_size:
                return sent

    # -- hooks ------------------------------------------------------------------------

    def install(self, *, logger: logging.Logger | None = None) -> "ErrorReporter":
        """Hook uncaught exceptions and ERROR logs. Idempotent; `uninstall` restores."""
        if self._installed:
            return self
        prev_sys, prev_thread = sys.excepthook, threading.excepthook

        def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
            if not issubclass(exc_type, KeyboardInterrupt):
                self.capture_exception(exc, handled=False, source="sys.excepthook", tb=tb)
            prev_sys(exc_type, exc, tb)

        def thread_hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
                self.capture_exception(args.exc_value, handled=False, source="thread", tb=args.exc_traceback)
            prev_thread(args)

        sys.excepthook = sys_hook
        threading.excepthook = thread_hook
        target = logger or logging.getLogger()
        self._handler = _ReporterHandler(self)
        target.addHandler(self._handler)
        self._installed = {"sys": prev_sys, "thread": prev_thread, "logger": target}
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        sys.excepthook = self._installed["sys"]
        threading.excepthook = self._installed["thread"]
        if self._handler is not None:
            self._installed["logger"].removeHandler(self._handler)
        self._installed, self._handler = {}, None


class _ReporterHandler(logging.Handler):
    """Forward ERROR+ log records. The reporter's own logger is ignored to
    avoid loops."""

    def __init__(self, reporter: ErrorReporter) -> None:
        super().__init__(level=logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                self.reporter.capture_exception(record.exc_info[1], handled=True, source=f"log:{record.name}",
                                                tb=record.exc_info[2])
            else:
                self.reporter.capture_message(record.getMessage(), level=record.levelname.lower(),
                                              logger=record.name, source="log")
        except Exception:                       # pragma: no cover - logging must never raise
            pass
