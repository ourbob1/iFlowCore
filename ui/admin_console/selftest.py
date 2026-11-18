"""
Admin Console self-test (`iFlowCoreAdmin --selftest [--json] [--out=report.json]`), run inside the
frozen build by scripts/package_desktop.py --app console. Everything happens in a temporary folder
on a free loopback port: nothing touches the operator's real accounts or signing key.

    runtime    version, bundled modules (the admin service, uvicorn, argon2, cryptography, boto3)
    service    the built-in service starts, answers /healthz, and only on 127.0.0.1
    accounts   first-run owner setup, sign-in, the forced password change for temporary passwords
    formats    save → publish a statement format → signed files in a folder → verified with the key
    ui         the sign-in and setup pages render
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path

from core.selftest import Report, _run

PASSWORD = secrets.token_urlsafe(18)          # a fresh throwaway account password on every run
SCHEMA = {"id": "selftest-format", "version": 1, "name": "Self-test", "platform": "Test",
          "rules": {"occurred_at": {"columns": ["Date"]}, "gross": {"columns": ["Fare"]}}}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run() -> Report:
    report = Report()
    with tempfile.TemporaryDirectory(prefix="iflow-admin-selftest-") as tmp:
        root = Path(tmp)
        os.environ["IFLOW_ADMIN_FORMATS_DIR"] = str(root / "cdn")
        from ui.admin_console.local_service import LocalService

        svc = LocalService(root / "data", port=_free_port())
        state: dict = {}

        def runtime(_: Path) -> str:
            from core import __version__
            import argon2  # noqa: F401
            import boto3  # noqa: F401
            import cryptography  # noqa: F401
            import iflow_ai
            import uvicorn  # noqa: F401

            for heavy in ("torch", "transformers"):
                if heavy in sys.modules:
                    raise RuntimeError(f"{heavy} was imported; the console must not load AI models")
            return f"console {__version__} · admin service {iflow_ai.__version__}"

        def service(_: Path) -> str:
            url = svc.start()
            if not svc.healthy() or svc._server.config.host != "127.0.0.1":
                raise RuntimeError("not healthy on loopback")
            return url

        def accounts(_: Path) -> str:
            from core.admin.client import AdminClient

            svc.create_first_owner("selftest", "Self-test", PASSWORD, PASSWORD)
            client, user = AdminClient.login(svc.url, "selftest", PASSWORD)
            if user["must_change_password"] or client.me()["role"] != "owner":
                raise RuntimeError("the first owner should be signed straight in")
            created = client.create_user("temp-user", "Temp", "support")
            temp_client, temp = AdminClient.login(svc.url, "temp-user", created["temporary_password"])
            if not temp["must_change_password"]:
                raise RuntimeError("temporary passwords must force a change")
            state["client"] = client
            return "owner setup, sign-in, forced change"

        def formats(_: Path) -> str:
            from core.financial.parsers.dynamic import verify_bundle

            client = state["client"]
            client.save_parser(SCHEMA)
            out = client.publish_parser(SCHEMA["id"])
            if "distribution" not in out:
                raise RuntimeError(f"not distributed: {out.get('distribution_error')}")
            bundle = (root / "cdn" / "v1" / "bundle.ifpb").read_text()
            verified = verify_bundle(bundle, [svc.public_key_hex()])
            return f"published and verified {verified.schemas[0].id} v{verified.schemas[0].version}"

        def ui(_: Path) -> str:
            from ui.admin_console.app import AdminConsoleApp

            class _Page:
                width, platform, route, title = 1280, "linux", "/", ""
                theme = dark_theme = theme_mode = None
                views: list = []
                services: list = []

                def __init__(self) -> None:
                    import flet as ft

                    self.platform_brightness = ft.Brightness.LIGHT
                    self.views = []

                def update(self) -> None:
                    pass

                def show_dialog(self, d) -> None:
                    pass

            page = _Page()
            fresh = LocalService(root / "fresh", port=svc.port)          # an empty folder: the setup page
            app = AdminConsoleApp(page, env={}, services={"local_service": fresh, "local_status": "ready"})
            fresh.start = lambda **kw: fresh.url                           # reuse the running server's port
            app.render()
            return "sign-in and setup pages render"

        for name, fn in (("runtime", runtime), ("service", service), ("accounts", accounts), ("formats", formats),
                         ("ui", ui)):
            _run(report, name, fn, root / "work")
        svc.stop()
        os.environ.pop("IFLOW_ADMIN_FORMATS_DIR", None)
    return report


def main(argv: list[str]) -> int:
    out = [a.split("=", 1)[1] for a in argv if a.startswith("--out=")]
    report = run()
    text = report.to_json() if "--json" in argv else report.to_text().replace("iFlow Core self-test",
                                                                              "iFlow Core Admin Console self-test")
    if out:
        Path(out[0]).write_text(text + "\n", encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if report.ok else 1
