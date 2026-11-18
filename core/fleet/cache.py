"""Encrypted-at-rest cache for raw decoder payloads (lives in the SQLCipher DB)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def _iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class DecodeCache:
    """Read-through cache keyed by (provider, vin, request_key)."""

    def __init__(self, db: Any, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._db = db
        self._clock = clock

    def get(self, provider: str, vin: str, request_key: str = "") -> dict[str, Any] | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM vehicle_decode_cache "
                "WHERE provider = ? AND vin = ? AND request_key = ? AND expires_at > ?",
                (provider, vin, request_key, _iso(self._clock())),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def put(self, provider: str, vin: str, payload: Any, *, ttl: timedelta, request_key: str = "") -> None:
        now = self._clock()
        with self._db.transaction() as conn:
            conn.execute(
                """INSERT INTO vehicle_decode_cache (provider, vin, request_key, payload_json, fetched_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (provider, vin, request_key) DO UPDATE SET
                       payload_json = excluded.payload_json,
                       fetched_at = excluded.fetched_at,
                       expires_at = excluded.expires_at""",
                (provider, vin, request_key, json.dumps(payload, separators=(",", ":")), _iso(now), _iso(now + ttl)),
            )

    def get_or_fetch(
        self, provider: str, vin: str, fetch: Callable[[], Any], *, ttl: timedelta, request_key: str = ""
    ) -> tuple[Any, bool]:
        """Return (payload, from_cache)."""
        cached = self.get(provider, vin, request_key)
        if cached is not None:
            return cached, True
        payload = fetch()
        self.put(provider, vin, payload, ttl=ttl, request_key=request_key)
        return payload, False

    def purge_expired(self) -> int:
        with self._db.transaction() as conn:
            return conn.execute(
                "DELETE FROM vehicle_decode_cache WHERE expires_at <= ?", (_iso(self._clock()),)
            ).rowcount
