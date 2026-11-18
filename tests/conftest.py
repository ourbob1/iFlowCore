"""Shared fixtures. Every DB test runs twice: plain SQLite and SQLCipher."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# Make the repo root importable without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import Database, load_sqlcipher_driver  # noqa: E402

TEST_KEY = "correct horse battery staple"
HAS_SQLCIPHER = load_sqlcipher_driver() is not None
requires_sqlcipher = pytest.mark.skipif(not HAS_SQLCIPHER, reason="SQLCipher driver not installed")


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("sqlcipher", id="sqlcipher", marks=requires_sqlcipher),
    ]
)
def db(request, tmp_path: Path):
    """An initialized Database on a temp file, for each driver."""
    if request.param == "sqlcipher":
        database = Database(tmp_path / "test.db", key=TEST_KEY, driver="sqlcipher")
    else:
        database = Database(tmp_path / "test.db", driver="sqlite")
    database.initialize()
    yield database
    database.close()


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def platform_id(db) -> str:
    pid = new_id()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO platforms (id, name, kind, default_currency) VALUES (?, ?, 'rideshare', 'USD')",
            (pid, "Uber"),
        )
    return pid


@pytest.fixture(autouse=True)
def _no_format_downloads(request, monkeypatch):
    """The app checks the iFlow CDN for statement formats after unlock; tests never go online.
    Tests of that path drive ui.viewmodels.formats.check() directly with a fake transport."""
    if "live_format_check" in request.keywords:
        return
    try:
        from ui.app import IFlowCoreApp
    except Exception:                                # the API/service suites don't import the UI
        return
    monkeypatch.setattr(IFlowCoreApp, "_update_formats", lambda self: None)
