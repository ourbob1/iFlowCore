"""Crash reporting: scrubbing, capture hooks, local log + queue, opt-in upload."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

import pytest

from core.telemetry import ErrorReporter, ReporterInfo, scrub_text
from core.telemetry.scrub import scrub_filename

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
INFO = ReporterInfo(release="0.6.0", environment="test", platform="linux")


@pytest.fixture
def reporter(tmp_path):
    return ErrorReporter(tmp_path / "logs", INFO, clock=lambda: NOW, extra_scrub_paths=[tmp_path])


# --- scrubbing -------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,leaked", [
    ("Can't open /home/alice/Receipts/scan.jpg", "alice"),
    ("C:\\Users\\Bob\\AppData\\Local\\iFlowCore\\x.db", "Bob"),
    ("fetch https://calendar.example.com/private/abc123token/basic.ics failed", "abc123token"),
    ("login alice@example.com failed", "alice@example.com"),
    ("api_key=sk_live_0123456789abcdef rejected", "sk_live_0123456789abcdef"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    ("home at 27.950575, -82.457178", "27.950575"),
    ("VIN 1HGCM82633A004352 decode failed", "1HGCM82633A004352"),
    ("amount $1,234.56 exceeds limit", "1,234.56"),
    ("card 4111 1111 1111 1111 declined", "4111 1111"),
    ("row 3f2b8c1e-9a7d-4e21-b6f0-2d9c8e7a1b34 missing", "3f2b8c1e"),
    ("key 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8", "5e884898da2804"),
])
def test_scrub_removes_personal_data(raw, leaked):
    out = scrub_text(raw)
    assert leaked not in out, out


def test_scrub_keeps_useful_text_and_caps_length():
    assert scrub_text("KeyError: 'category'") == "KeyError: 'category'"
    assert scrub_text("division by zero") == "division by zero"
    assert len(scrub_text("x" * 5000)) == 500


def test_scrub_extra_paths(tmp_path):
    data = tmp_path / "iFlowCore"
    assert str(tmp_path) not in scrub_text(f"locked: {data}/iflowcore.db", extra_paths=[data])


def test_scrub_filename_is_relative():
    import core.telemetry.reporter as mod

    name, in_app = scrub_filename(mod.__file__)
    assert (name, in_app) == ("core/telemetry/reporter.py", True)
    name, in_app = scrub_filename(json.__file__)
    assert not in_app and name.endswith("json/__init__.py") and not name.startswith("/")


# --- capture ---------------------------------------------------------------------------------


def _raise(msg: str = "boom at /home/alice/x.csv") -> None:
    raise ValueError(msg)


def test_capture_exception_writes_log_and_queue(reporter):
    try:
        _raise()
    except ValueError as exc:
        event = reporter.capture_exception(exc)
    assert event["type"] == "ValueError" and "alice" not in event["message"]
    assert event["frames"][-1]["function"] == "_raise" and event["frames"][-1]["in_app"]
    assert event["frames"][-1]["file"] == "tests/test_telemetry.py"
    assert "locals" not in json.dumps(event)
    assert reporter.recent()[0]["id"] == event["id"]
    assert [e["id"] for e in reporter.pending()] == [event["id"]]
    assert event["release"] == "0.6.0" and len(event["install_id"]) == 36


def test_same_crash_is_counted_not_duplicated(reporter):
    for i in range(5):
        try:
            _raise(f"boom {i}")
        except ValueError as exc:
            reporter.capture_exception(exc)
    queue = reporter.pending()
    assert len(queue) == 1 and queue[0]["count"] == 5
    assert len(reporter.recent(50)) == 5                     # the local log keeps every occurrence


def test_fingerprint_differs_by_location(reporter):
    def other() -> None:
        raise ValueError("x")

    for fn in (_raise, other):
        try:
            fn()
        except ValueError as exc:
            reporter.capture_exception(exc)
    assert len({e["fingerprint"] for e in reporter.pending()}) == 2


def test_queue_is_bounded(tmp_path):
    r = ErrorReporter(tmp_path, INFO, clock=lambda: NOW, max_queue=3)
    for i in range(6):
        r.capture_message(f"distinct message kind {chr(65 + i)}")
    assert len(r.pending()) == 3


def test_log_rotates(tmp_path):
    r = ErrorReporter(tmp_path, INFO, clock=lambda: NOW, max_log_bytes=2000)
    for i in range(20):
        r.capture_message(f"message {chr(65 + i)}")
    assert (tmp_path / "errors.jsonl.1").exists()
    assert (tmp_path / "errors.jsonl").stat().st_size < 4000


def test_install_id_is_stable(tmp_path):
    a = ErrorReporter(tmp_path, INFO).install_id
    assert ErrorReporter(tmp_path, INFO).install_id == a


def test_torn_queue_line_is_skipped(reporter):
    reporter.capture_message("first")
    with reporter.queue_path.open("a") as fh:
        fh.write('{"half": ')
    assert len(reporter.pending()) == 1


def test_context_and_cause(reporter):
    reporter.context["route"] = "/finances"
    try:
        try:
            _raise("inner")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        event = reporter.capture_exception(exc)
    assert event["context"] == {"route": "/finances"}
    assert event["cause"].startswith("ValueError")


# --- hooks -----------------------------------------------------------------------------------


def test_logging_handler_captures_errors_only(reporter):
    logger = logging.getLogger("iflowcore.test.hooks")
    reporter.install(logger=logger)
    try:
        logger.warning("just a warning")
        try:
            _raise()
        except ValueError:
            logger.exception("View failed")
        logger.error("plain error %s", "text")
    finally:
        reporter.uninstall()
    events = reporter.recent()
    assert [e["kind"] for e in events] == ["message", "exception"]
    assert events[1]["source"] == "log:iflowcore.test.hooks"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_hook_captures_background_crash(reporter):
    import sys

    before = (sys.excepthook, threading.excepthook)
    reporter.install(logger=logging.getLogger("iflowcore.test.thread"))
    try:
        t = threading.Thread(target=_raise)
        t.start()
        t.join()
    finally:
        reporter.uninstall()
    assert (sys.excepthook, threading.excepthook) == before
    assert reporter.recent()[0]["source"] == "thread"
    assert reporter.recent()[0]["handled"] is False


def test_reporter_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    r = ErrorReporter(blocker / "logs", INFO)
    assert r.capture_message("x") is None                    # can't write; swallowed


# --- upload ----------------------------------------------------------------------------------


def test_flush_sends_in_batches_and_clears(reporter):
    for i in range(7):
        reporter.capture_message(f"issue {chr(65 + i)}")
    batches = []

    def sender(events):
        batches.append(len(events))
        return len(events)

    assert reporter.flush(sender, batch_size=3) == 7
    assert batches == [3, 3, 1] and reporter.pending() == []


def test_flush_failure_keeps_queue(reporter):
    reporter.capture_message("keep me")

    def sender(events):
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError):
        reporter.flush(sender)
    assert len(reporter.pending()) == 1


def test_clear(reporter):
    reporter.capture_message("x")
    reporter.clear()
    assert reporter.pending() == [] and reporter.recent() == []
