"""Crash reporting and error logging (local-first, opt-in upload). See reporter.py."""

from core.telemetry.reporter import ErrorReporter, ReporterInfo
from core.telemetry.scrub import scrub_text

__all__ = ["ErrorReporter", "ReporterInfo", "scrub_text"]
