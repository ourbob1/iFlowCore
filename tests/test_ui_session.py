"""The app shell inside a real Flet session (not the stub page).

Flet diffs every `page.update()` against snapshots recorded while encoding the previous message,
and reconciles list items that carry the same explicit `key`. A rebuilt control that is matched to
its predecessor by key is diffed "frozen" and stays immutable: assigning to it later raises
"Frozen controls cannot be updated." These tests drive the real diff/encode path the web and
desktop clients use, so that class of bug shows up here instead of on a user's screen.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import msgpack
import pytest

from flet.controls.base_control import BaseControl
from flet.messaging.connection import Connection
from flet.messaging.protocol import configure_encode_object_for_msgpack
from flet.messaging.session import Session
from tests.conftest import requires_sqlcipher
from ui.app import IFlowCoreApp


class _Conn(Connection):
    """Encodes like the real transports (encoding records Flet's diff snapshots)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[Any] = []
        self._encode = configure_encode_object_for_msgpack(BaseControl)

    def send_message(self, message: Any) -> None:
        msgpack.packb([message.action, message.body], default=self._encode)
        self.messages.append(message)

    @property
    def pubsubhub(self) -> Any:
        from flet.pubsub.pubsub_hub import PubSubHub

        return PubSubHub()


def _walk(control: Any, seen: set[int] | None = None):
    seen = set() if seen is None else seen
    if id(control) in seen:
        return
    seen.add(id(control))
    yield control
    for f in dataclasses.fields(control):
        if f.name.startswith("_") or f.name == "parent":
            continue
        value = getattr(control, f.name, None)
        for item in value if isinstance(value, list) else [value]:
            if dataclasses.is_dataclass(item) and isinstance(item, BaseControl):
                yield from _walk(item, seen)


def _find(page: Any, key: str) -> Any:
    return next((c for c in _walk(page.views[-1]) if getattr(c, "key", None) == key), None)


@pytest.fixture
def session(tmp_path: Path):
    """A connected Flet session running the app in production mode, locked (encrypted DB exists)."""
    from database.connection import Database

    existing = Database(tmp_path / "iflowcore.db", key="the real passphrase 42", driver="sqlcipher")
    existing.initialize()
    existing.close()
    conn = _Conn()
    sess = Session(conn)
    page = sess.page
    page.width, page.height = 1280, 800
    app = IFlowCoreApp(page, env={"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "production",
                                 "IFLOW_PLATFORM": "linux"})
    app.start()
    msgpack.packb(sess.get_page_patch(), default=configure_encode_object_for_msgpack(BaseControl))  # client registers
    return sess, page, app


def _frozen(page: Any) -> list[str]:
    return [f"{type(c).__name__}({getattr(c, 'key', None)})" for c in _walk(page.views[-1]) if hasattr(c, "_frozen")]


@requires_sqlcipher
class TestRerenders:
    @pytest.mark.parametrize("renders", [1, 2, 5])
    def test_rerender_leaves_controls_mutable(self, session, renders):
        _, page, app = session
        for _ in range(renders):
            app.render()                    # what resize / brightness / route events do
        assert _frozen(page) == []
        field = _find(page, "passphrase")
        field.value = "changed"             # would raise "Frozen controls cannot be updated."
        field.update()

    def test_wrong_passphrase_after_rerender_shows_error(self, session):
        """The reported crash: brightness event on connect → re-render → wrong passphrase → crash."""
        sess, page, app = session
        app.render()
        _find(page, "passphrase").value = "not the passphrase"
        asyncio.run(sess.dispatch_event(_find(page, "btn-unlock")._i, "click", None))
        crashed = [m for m in sess.connection.messages if "CRASH" in str(m.action).upper()]
        assert crashed == []
        texts = [c.value for c in _walk(page.views[-1]) if type(c).__name__ == "Text"]
        assert any("didn't unlock" in (t or "") for t in texts)
        assert _find(page, "passphrase").value == ""

    def test_rerender_still_patches_the_client(self, session):
        sess, page, app = session
        before = len(sess.connection.messages)
        app.render()
        assert len(sess.connection.messages) > before


@requires_sqlcipher
class TestUnlockedShell:
    @pytest.mark.parametrize("width", [390, 1280])
    def test_every_route_rerenders_mutable(self, tmp_path, width):
        from ui import router

        conn = _Conn()
        sess = Session(conn)
        page = sess.page
        page.width, page.height = width, 800
        app = IFlowCoreApp(page, env={"IFLOW_DATA_DIR": str(tmp_path), "IFLOW_ENV": "production",
                                     "IFLOW_PLATFORM": "linux"},
                          services={"run_background": lambda fn: fn()})     # no event loop in this harness
        app.start()
        msgpack.packb(sess.get_page_patch(), default=configure_encode_object_for_msgpack(BaseControl))
        _find(page, "passphrase").value = _find(page, "confirm").value = "a long enough test passphrase"
        asyncio.run(sess.dispatch_event(_find(page, "btn-unlock")._i, "click", None))
        assert app.state.unlocked
        for spec in router.ROUTES:
            if spec.path == router.UNLOCK:
                continue
            app.navigate(spec.path)
            app.render()
            app.render()
            assert _frozen(page) == [], spec.path
        assert not [m for m in conn.messages if "CRASH" in str(m.action).upper()]
