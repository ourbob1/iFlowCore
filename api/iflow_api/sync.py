"""
End-to-end encrypted sync (Phase 10.3). The server is a zero-knowledge store:

- keys      one opaque JSON envelope per account (the data key wrapped by the user's passphrase
            and recovery key, client-side). Replaced only with the version the client last saw.
- push      records {collection, record_id, base_version, ciphertext}. Accepted when base_version
            equals the stored version (0 = new); the record gets version+1 and the account's next
            sequence number. Otherwise the server's copy comes back as a conflict.
- pull      records with seq > cursor, in seq order, paged.
- reset     deletes the envelope and every record (e.g. passphrase and recovery key both lost);
            needs a sign-in within the last 10 minutes.

`collection` is an HMAC of the table name and `record_id` a random UUID, so the server learns
neither what kind of data a record holds nor anything inside it. Sequence numbers are allocated
under a row lock on the account's sync_state, so concurrent pushes from two devices serialise.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .db import SyncKeys, SyncRecord, SyncState
from .security import AccessClaims
from .service import AuthError

MAX_ENVELOPE = 8 * 1024
MAX_ITEMS = 200
MAX_RECORD = 256 * 1024
MAX_RECORDS = 200_000
MAX_BYTES = 512 * 1024 * 1024
PULL_LIMIT = 500
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class PushItem:
    collection: str
    record_id: str
    base_version: int
    ciphertext: bytes


def decode_item(raw: dict[str, Any]) -> PushItem:
    collection, record_id = str(raw.get("collection", "")), str(raw.get("record_id", ""))
    if not _ID.match(collection) or not _ID.match(record_id):
        raise AuthError("invalid_record", "Invalid collection or record id")
    base = raw.get("base_version")
    if not isinstance(base, int) or base < 0:
        raise AuthError("invalid_record", "base_version must be a non-negative integer")
    try:
        blob = base64.b64decode(str(raw.get("ciphertext", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthError("invalid_record", "ciphertext must be base64") from exc
    if not 16 <= len(blob) <= MAX_RECORD:
        raise AuthError("invalid_record", "Record size out of range")
    return PushItem(collection, record_id, base, blob)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class SyncService:
    def __init__(self, sessions: sessionmaker, clock: Callable, fresh_auth_s: int) -> None:
        self._sessions, self.clock, self.fresh_auth_s = sessions, clock, fresh_auth_s

    # --- keys -------------------------------------------------------------------------------------

    def get_keys(self, claims: AccessClaims) -> dict[str, Any]:
        with self._sessions() as db:
            row = db.get(SyncKeys, claims.user_id)
            if row is None:
                raise AuthError("no_keys", "Sync isn't set up for this account yet", 404)
            return {"envelope": json.loads(row.envelope), "key_version": row.key_version}

    def put_keys(self, claims: AccessClaims, envelope: dict[str, Any], expected_version: int | None) -> int:
        text = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        if not isinstance(envelope, dict) or len(text) > MAX_ENVELOPE:
            raise AuthError("invalid_keys", "Key envelope must be a JSON object under 8 KB")
        with self._sessions.begin() as db:
            self._lock_state(db, claims.user_id)
            row = db.get(SyncKeys, claims.user_id)
            current = row.key_version if row else None
            if current != expected_version:
                raise AuthError("key_conflict", "Sync keys changed on another device; reload them", 409)
            if row is None:
                row = SyncKeys(user_id=claims.user_id, envelope=text, key_version=1, updated_at=self.clock())
                db.add(row)
            else:
                row.envelope, row.key_version, row.updated_at = text, row.key_version + 1, self.clock()
            return row.key_version

    # --- records ----------------------------------------------------------------------------------

    def push(self, claims: AccessClaims, raw_items: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
            raise AuthError("invalid_request", f"Push at most {MAX_ITEMS} records at a time")
        items = [decode_item(r) for r in raw_items]
        if len({(i.collection, i.record_id) for i in items}) != len(items):
            raise AuthError("invalid_request", "Duplicate record in one push")
        results = []
        now = self.clock()
        with self._sessions.begin() as db:
            if db.get(SyncKeys, claims.user_id) is None:
                raise AuthError("no_keys", "Set up sync keys before pushing", 409)
            state = self._lock_state(db, claims.user_id)
            for item in items:
                row = db.get(SyncRecord, (claims.user_id, item.collection, item.record_id))
                current = row.version if row else 0
                if item.base_version != current:
                    results.append({"collection": item.collection, "record_id": item.record_id, "status": "conflict",
                                    "version": current, "seq": row.seq if row else 0,
                                    "ciphertext": _b64(row.ciphertext) if row else None})
                    continue
                grow = len(item.ciphertext) - (len(row.ciphertext) if row else 0)
                if state.bytes_used + grow > MAX_BYTES or (row is None and state.record_count >= MAX_RECORDS):
                    raise AuthError("quota_exceeded", "This account's sync storage is full", 413)
                state.last_seq += 1
                state.bytes_used += grow
                if row is None:
                    state.record_count += 1
                    row = SyncRecord(user_id=claims.user_id, collection=item.collection, record_id=item.record_id,
                                     version=0, seq=0, ciphertext=b"", updated_at=now)
                    db.add(row)
                row.version, row.seq, row.ciphertext, row.updated_at = current + 1, state.last_seq, item.ciphertext, now
                results.append({"collection": item.collection, "record_id": item.record_id, "status": "ok",
                                "version": row.version, "seq": row.seq})
            return {"results": results, "cursor": state.last_seq}

    def pull(self, claims: AccessClaims, after: int, limit: int) -> dict[str, Any]:
        limit = max(1, min(limit, PULL_LIMIT))
        with self._sessions() as db:
            rows = db.scalars(select(SyncRecord).where(SyncRecord.user_id == claims.user_id, SyncRecord.seq > after)
                              .order_by(SyncRecord.seq).limit(limit + 1)).all()
            more = len(rows) > limit
            rows = rows[:limit]
            return {"items": [{"collection": r.collection, "record_id": r.record_id, "version": r.version,
                               "seq": r.seq, "ciphertext": _b64(r.ciphertext)} for r in rows],
                    "cursor": rows[-1].seq if rows else after, "more": more}

    def status(self, claims: AccessClaims) -> dict[str, Any]:
        with self._sessions() as db:
            state = db.get(SyncState, claims.user_id)
            keys = db.get(SyncKeys, claims.user_id)
            return {"keys": keys is not None, "key_version": keys.key_version if keys else None,
                    "records": state.record_count if state else 0, "bytes": state.bytes_used if state else 0,
                    "cursor": state.last_seq if state else 0}

    def reset(self, claims: AccessClaims) -> None:
        if (self.clock() - claims.auth_time).total_seconds() > self.fresh_auth_s:
            raise AuthError("reauth_required", "Sign in again to confirm erasing synced data", 403)
        with self._sessions.begin() as db:
            self._lock_state(db, claims.user_id)
            db.execute(delete(SyncRecord).where(SyncRecord.user_id == claims.user_id))
            db.execute(delete(SyncKeys).where(SyncKeys.user_id == claims.user_id))
            state = db.get(SyncState, claims.user_id)
            state.record_count, state.bytes_used = 0, 0          # seq keeps growing: cursors stay monotonic

    # --- helpers ----------------------------------------------------------------------------------

    def _lock_state(self, db, user_id: str) -> SyncState:
        state = db.scalars(select(SyncState).where(SyncState.user_id == user_id).with_for_update()).first()
        if state is None:
            state = SyncState(user_id=user_id, last_seq=0, record_count=0, bytes_used=0)
            db.add(state)
            db.flush()
        return state

    def recount(self, user_id: str) -> None:                  # maintenance helper
        with self._sessions.begin() as db:
            state = self._lock_state(db, user_id)
            state.record_count = db.scalar(select(func.count()).select_from(SyncRecord)
                                           .where(SyncRecord.user_id == user_id)) or 0
