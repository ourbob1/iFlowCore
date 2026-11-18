"""
The web build's encrypted storage ("vault").

In the browser, Python's filesystem is in memory and SQLCipher doesn't exist. The app keeps its
SQLite database in that in-memory filesystem while unlocked, and after every commit saves an
encrypted snapshot to the browser's private origin storage (OPFS). Nothing readable is ever
written to browser storage; the passphrase is the only way back in (same model as desktop).

Slot format (two slots, written alternately so an interrupted save can't destroy the last good one):

    header  "IFCV" | format u8 | kdf u8 (1 = Argon2id) | t u8 | m u32 KiB | p u8 | salt 16 | gen u64 | nonce 12
    body    AES-256-GCM(key, zip{iflowcore.db, preferences.json}, AAD = header)

key = Argon2id(passphrase, salt, t, m, p), derived once at unlock. The newest slot that
authenticates wins; a slot that fails authentication (torn write, tampering) is ignored.
"""

from __future__ import annotations

import io
import os
import struct
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

MAGIC = b"IFCV"
FORMAT = 1
KDF_ARGON2ID = 1
PARAMS = {"t": 3, "m": 64 * 1024, "p": 1}
_HEADER = struct.Struct(">4sBBBIB16sQ12s")
MIN_PASSPHRASE = 10


class VaultError(Exception):
    pass


class WrongPassphrase(VaultError):
    pass


class VaultBusy(VaultError):
    """Another browser tab already has this vault open."""


class SlotStorage(Protocol):
    def read(self, slot: int) -> bytes | None: ...
    def write(self, slot: int, data: bytes) -> None: ...


class MemoryStorage:
    def __init__(self) -> None:
        self.slots: dict[int, bytes] = {}

    def read(self, slot: int) -> bytes | None:
        return self.slots.get(slot)

    def write(self, slot: int, data: bytes) -> None:
        self.slots[slot] = data


@dataclass(frozen=True)
class _Header:
    t: int
    m: int
    p: int
    salt: bytes
    generation: int
    nonce: bytes

    def pack(self) -> bytes:
        return _HEADER.pack(MAGIC, FORMAT, KDF_ARGON2ID, self.t, self.m, self.p, self.salt, self.generation, self.nonce)

    @classmethod
    def parse(cls, blob: bytes) -> _Header | None:
        if len(blob) < _HEADER.size + 16:
            return None
        magic, fmt, kdf, t, m, p, salt, gen, nonce = _HEADER.unpack_from(blob)
        if magic != MAGIC or fmt != FORMAT or kdf != KDF_ARGON2ID:
            return None
        if not (1 <= t <= 10 and 8 * 1024 <= m <= 1024 * 1024 and 1 <= p <= 8):
            return None                                   # refuse parameters we'd never write
        return cls(t, m, p, salt, gen, nonce)


def _derive(passphrase: str, salt: bytes, t: int, m: int, p: int) -> bytes:
    return Argon2id(salt=salt, length=32, iterations=t, lanes=p, memory_cost=m).derive(passphrase.encode("utf-8"))


def _pack_files(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buf.getvalue()


def _unpack_files(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist() if "/" not in name and ".." not in name}


class Vault:
    def __init__(self, storage: SlotStorage) -> None:
        self.storage = storage
        self._key: bytes | None = None
        self._params: tuple[int, int, int, bytes] | None = None
        self.generation = 0

    def _slots(self) -> list[tuple[_Header, bytes]]:
        found = []
        for slot in (0, 1):
            blob = self.storage.read(slot)
            header = _Header.parse(blob) if blob else None
            if header is not None:
                found.append((header, blob))
        return sorted(found, key=lambda hb: hb[0].generation, reverse=True)

    def exists(self) -> bool:
        return bool(self._slots())

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def create(self, passphrase: str, files: dict[str, bytes]) -> None:
        if len(passphrase or "") < MIN_PASSPHRASE:
            raise VaultError(f"Use at least {MIN_PASSPHRASE} characters")
        salt = os.urandom(16)
        self._key = _derive(passphrase, salt, PARAMS["t"], PARAMS["m"], PARAMS["p"])
        self._params = (PARAMS["t"], PARAMS["m"], PARAMS["p"], salt)
        self.generation = 0
        self.save(files)

    def unlock(self, passphrase: str) -> dict[str, bytes]:
        slots = self._slots()
        if not slots:
            raise VaultError("Nothing is stored in this browser yet")
        keys: dict[tuple[int, int, int, bytes], bytes] = {}
        for header, blob in slots:                        # newest first; fall back to the older slot
            params = (header.t, header.m, header.p, header.salt)
            if params not in keys:
                keys[params] = _derive(passphrase, header.salt, header.t, header.m, header.p)
            try:
                plain = AESGCM(keys[params]).decrypt(header.nonce, blob[_HEADER.size:], blob[:_HEADER.size])
            except InvalidTag:
                continue
            self._key, self._params, self.generation = keys[params], params, header.generation
            return _unpack_files(plain)
        raise WrongPassphrase("That passphrase doesn't unlock the data saved in this browser")

    def save(self, files: dict[str, bytes]) -> None:
        if self._key is None or self._params is None:
            raise VaultError("vault is locked")
        t, m, p, salt = self._params
        header = _Header(t, m, p, salt, self.generation + 1, os.urandom(12))
        packed = header.pack()
        self.storage.write(header.generation % 2, packed + AESGCM(self._key).encrypt(header.nonce, _pack_files(files),
                                                                                    packed))
        self.generation = header.generation

    def change_passphrase(self, new_passphrase: str, files: dict[str, bytes]) -> None:
        """Re-key: new salt and key; both slots rewritten so the old passphrase opens nothing."""
        if len(new_passphrase or "") < MIN_PASSPHRASE:
            raise VaultError(f"Use at least {MIN_PASSPHRASE} characters")
        salt = os.urandom(16)
        self._key = _derive(new_passphrase, salt, PARAMS["t"], PARAMS["m"], PARAMS["p"])
        self._params = (PARAMS["t"], PARAMS["m"], PARAMS["p"], salt)
        self.save(files)
        self.save(files)

    def lock(self) -> None:
        self._key = self._params = None


class OpfsStorage:
    """Origin Private File System, via synchronous access handles (dedicated workers only, which is
    where Flet runs Python). Opening takes an exclusive lock per file, so a second tab can't
    overwrite the vault underneath the first."""

    def __init__(self, handles: list[Any]) -> None:
        self._handles = handles

    @classmethod
    async def open(cls, name: str = "iflowcore.vault") -> OpfsStorage:   # pragma: no cover - browser only
        import js  # type: ignore[import-not-found]
        from pyodide.ffi import JsException, to_js  # type: ignore[import-not-found]

        opts = to_js({"create": True}, dict_converter=js.Object.fromEntries)
        try:
            root = await js.navigator.storage.getDirectory()
            handles = []
            for slot in (0, 1):
                fh = await root.getFileHandle(f"{name}.{slot}", opts)
                handles.append(await fh.createSyncAccessHandle())
        except JsException as exc:
            if "NoModificationAllowed" in str(exc) or "lock" in str(exc).lower():
                raise VaultBusy("iFlow Core is already open in another tab of this browser") from exc
            raise VaultError(f"This browser can't store data for iFlow Core ({exc})") from exc
        try:                                    # ask the browser not to evict our data under storage pressure
            await js.navigator.storage.persist()
        except Exception:
            pass
        return cls(handles)

    def read(self, slot: int) -> bytes | None:   # pragma: no cover - browser only
        import js  # type: ignore[import-not-found]
        from pyodide.ffi import to_js  # type: ignore[import-not-found]

        h = self._handles[slot]
        size = int(h.getSize())
        if size == 0:
            return None
        buf = js.Uint8Array.new(size)
        h.read(buf, to_js({"at": 0}, dict_converter=js.Object.fromEntries))
        return bytes(buf.to_bytes())

    def write(self, slot: int, data: bytes) -> None:   # pragma: no cover - browser only
        import js  # type: ignore[import-not-found]
        from pyodide.ffi import to_js  # type: ignore[import-not-found]

        h = self._handles[slot]
        h.truncate(0)
        h.write(to_js(data), to_js({"at": 0}, dict_converter=js.Object.fromEntries))
        h.flush()
