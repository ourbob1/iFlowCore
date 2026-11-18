"""
The Admin Console's built-in service: the same admin API as services/ai (accounts, licenses, Parser
Studio, CDN publishing), running inside the console on 127.0.0.1 so an operator never has to start
anything from a terminal. No AI models are loaded; the receipt/tax endpoints aren't used here.

State lives in the operator's data folder (default ~/.local/share/iflow-ai, the folder the service
has always used, so existing console accounts keep working):

    admin.db               console accounts, sessions, audit trail, licenses, statement formats
    telemetry.db           crash reports (only if apps are pointed at this machine)
    vendor-signing.key     the Ed25519 signing key for licenses and statement formats (0600).
                           Apps trust its public key; BACK IT UP. Losing it means new app builds.
    console.json           publishing settings (CDN bucket, distribution, AWS profile): no secrets

Only loopback: the port isn't reachable from other machines.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 18765
KEY_FILE = "vendor-signing.key"
CONFIG_FILE = "console.json"


def default_data_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("IFLOW_ADMIN_DATA_DIR"):
        return Path(env["IFLOW_ADMIN_DATA_DIR"]).expanduser()
    base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "iflow-ai"


@dataclass
class PublishingSettings:
    formats_bucket: str = ""
    formats_distribution_id: str = ""
    formats_public_url: str = "https://formats.iflowcore.com/v1/bundle.ifpb"
    aws_profile: str = "iflow"


class LocalServiceError(Exception):
    pass


_START_LOCK = threading.Lock()
_SHARED: dict[tuple[str, int], "LocalService"] = {}


class LocalService:
    def __init__(self, data_dir: Path | None = None, *, port: int = PORT) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.port = port
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @classmethod
    def shared(cls, data_dir: Path | None = None, *, port: int = PORT) -> "LocalService":
        """One service per data folder and port in this process (every console window shares it)."""
        folder = Path(data_dir) if data_dir else default_data_dir()
        key = (str(folder), port)
        with _START_LOCK:
            if key not in _SHARED:
                _SHARED[key] = cls(folder, port=port)
            return _SHARED[key]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    # -- the signing key -----------------------------------------------------------------------------

    @property
    def key_path(self) -> Path:
        return self.data_dir / KEY_FILE

    def signing_key_hex(self) -> str:
        """The vendor signing key, created on first use (never logged or shown)."""
        path = self.key_path
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if len(value) != 64:
                raise LocalServiceError(f"{path} is damaged (expected 64 hex characters)")
            bytes.fromhex(value)
            return value
        self.data_dir.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(value + "\n")
        log.info("Created a new vendor signing key at %s", path)
        return value

    def public_key_hex(self) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.signing_key_hex()))
        return key.public_key().public_bytes_raw().hex()

    # -- publishing settings ---------------------------------------------------------------------------

    def load_publishing(self) -> PublishingSettings:
        path = self.data_dir / CONFIG_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return PublishingSettings()
        known = {k: str(v) for k, v in raw.items() if k in PublishingSettings.__dataclass_fields__}
        return PublishingSettings(**known)

    def save_publishing(self, settings: PublishingSettings) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / CONFIG_FILE).write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")

    # -- first run -------------------------------------------------------------------------------------

    def needs_setup(self) -> bool:
        """No console accounts yet: the console offers to create the first owner."""
        from iflow_ai.admin_users import AdminUsers

        users = AdminUsers(self.data_dir / "admin.db")
        try:
            return users.count() == 0
        finally:
            users.close()

    def create_first_owner(self, username: str, name: str, password: str, confirm: str) -> None:
        """Only while there are no accounts at all, and only on this computer (no network involved)."""
        from iflow_ai.admin_users import AccountError, AdminUsers

        if password != confirm:
            raise LocalServiceError("The passwords don't match")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        users = AdminUsers(self.data_dir / "admin.db")
        try:
            if users.count():
                raise LocalServiceError("This computer already has console accounts; sign in instead")
            users.create(username, name or username, "owner", created_by="first-run setup", temp_password=password,
                         must_change=False)
        except AccountError as exc:
            raise LocalServiceError(exc.message) from None
        finally:
            users.close()
        self.signing_key_hex()                              # create the signing key now, not mid-publish

    # -- lifecycle ---------------------------------------------------------------------------------------

    def healthy(self, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/healthz", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _port_free(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, self.port))
                return True
            except OSError:
                return False

    def build_app(self) -> Any:
        from iflow_ai.app import create_app
        from iflow_ai.format_publisher import DirectoryPublisher, S3Publisher
        from iflow_ai.models import ModelRegistry
        from iflow_ai.settings import Settings

        self.data_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings.from_env({
            "RS_AI_TOKENS": secrets.token_hex(32),          # client routes unused here; random each launch
            "RS_AI_DATA_DIR": str(self.data_dir),
            "RS_AI_LICENSE_SIGNING_KEY": self.signing_key_hex(),
            "RS_AI_PRELOAD": "0",
        })
        registry = ModelRegistry()
        for name in ("llm", "vlm", "ocr"):
            registry.register(name, None)                   # no models in the console
        pub = self.load_publishing()
        publisher: Any = None
        if pub.formats_bucket:
            publisher = S3Publisher(pub.formats_bucket, distribution_id=pub.formats_distribution_id or None,
                                    public_url=pub.formats_public_url or None, profile=pub.aws_profile or None)
        elif os.environ.get("IFLOW_ADMIN_FORMATS_DIR"):       # testing / self-hosting to a folder
            publisher = DirectoryPublisher(os.environ["IFLOW_ADMIN_FORMATS_DIR"], pub.formats_public_url or None)
        return create_app(settings, registry=registry, publisher=publisher)   # None: no CDN target

    def start(self, *, wait: float = 15.0) -> str:
        """Start (or reuse) the service; returns its URL. Raises LocalServiceError."""
        with _START_LOCK:                                   # two windows opening at once start it once
            return self._start(wait)

    def _start(self, wait: float) -> str:
        if self.healthy():
            return self.url                                 # already running (another console window)
        if self._thread is not None and self._thread.is_alive():
            return self._wait(wait)                         # starting in this process right now
        if not self._port_free():
            raise LocalServiceError(f"Port {self.port} on this computer is used by another program")
        import uvicorn

        config = uvicorn.Config(self.build_app(), host=HOST, port=self.port, log_level="warning",
                                access_log=False, server_header=False, lifespan="on", loop="asyncio",
                                http="h11")
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None   # runs in a thread
        self._thread = threading.Thread(target=self._server.run, name="iflow-admin-service", daemon=True)
        self._thread.start()
        return self._wait(wait)

    def _wait(self, wait: float) -> str:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.healthy(0.5):
                return self.url
            if self._thread is None or not self._thread.is_alive():
                break
            time.sleep(0.1)
        raise LocalServiceError("The built-in service didn't start; see the console log")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def restart(self) -> str:
        self.stop()
        return self.start()
