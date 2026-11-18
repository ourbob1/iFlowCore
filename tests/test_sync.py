"""Phase 10.3: end-to-end encrypted sync (crypto, zero-knowledge server API, multi-device engine).

Server tests also run against PostgreSQL when IFLOW_API_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest
from sqlalchemy import text

from core.account import AccountClient, MemorySessionStore
from core.device_settings import SettingsStore
from core.sync.crypto import (
    SyncCryptoError,
    SyncKeys,
    WrongPassphrase,
    format_recovery_key,
    parse_recovery_key,
)
from core.sync.engine import LOCAL_ONLY_TABLES, NATURAL_KEYS, SyncEngine, SyncError, synced_tables
from core.sync.manager import SyncManager
from database.connection import Database

from .test_account_api import PASSWORD, PG_URL, Harness
from .test_account_client import API, AppTransport

SYNC_PASS = "sync passphrase for tests"


# --- crypto ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def made():
    return SyncKeys.create("acct-1", SYNC_PASS)


class TestCrypto:
    def test_unlock_and_recover(self, made):
        keys, env, rk = made
        assert SyncKeys.unlock("acct-1", env, SYNC_PASS).adk == keys.adk
        assert SyncKeys.recover("acct-1", env, rk).adk == keys.adk
        assert SyncKeys.recover("acct-1", env, rk.lower().replace("-", " ")).adk == keys.adk   # forgiving input

    def test_wrong_passphrase_or_account(self, made):
        _, env, rk = made
        with pytest.raises(WrongPassphrase):
            SyncKeys.unlock("acct-1", env, "not the passphrase")
        with pytest.raises(WrongPassphrase):
            SyncKeys.unlock("acct-2", env, SYNC_PASS)                    # envelope bound to its account
        with pytest.raises(WrongPassphrase):
            SyncKeys.recover("acct-1", env, format_recovery_key(os.urandom(32)))

    def test_recovery_key_typos_are_caught(self, made):
        rk = made[2]
        i = rk.index("-") - 1
        typo = rk[:i] + ("A" if rk[i] != "A" else "B") + rk[i + 1:]
        with pytest.raises(SyncCryptoError, match="typo"):
            parse_recovery_key(typo)
        with pytest.raises(SyncCryptoError):
            parse_recovery_key("too short")
        for _ in range(100):
            raw = os.urandom(32)
            assert parse_recovery_key(format_recovery_key(raw)) == raw

    def test_envelope_never_contains_the_key(self, made):
        keys, env, rk = made
        blob = json.dumps(env)
        assert base64.b64encode(keys.adk).decode() not in blob and keys.adk.hex() not in blob
        assert rk.replace("-", "") not in blob

    def test_hostile_envelope_parameters(self, made):
        _, env, _ = made
        for bad in ({**env, "m": 4 * 1024 * 1024}, {**env, "t": 0}, {**env, "v": 2}, {**env, "kdf": "pbkdf2"}):
            with pytest.raises(SyncCryptoError):
                SyncKeys.unlock("acct-1", bad, SYNC_PASS)

    def test_short_passphrase(self):
        with pytest.raises(SyncCryptoError, match="10 characters"):
            SyncKeys.create("acct-1", "short")

    def test_rewrap_keeps_recovery(self, made):
        keys, env, rk = made
        env2 = keys.rewrap_passphrase(env, "a whole new passphrase")
        assert SyncKeys.unlock("acct-1", env2, "a whole new passphrase").adk == keys.adk
        assert SyncKeys.recover("acct-1", env2, rk).adk == keys.adk
        with pytest.raises(WrongPassphrase):
            SyncKeys.unlock("acct-1", env2, SYNC_PASS)

    def test_records_are_bound_to_their_label(self, made):
        keys = made[0]
        blob = keys.seal("c1", "r1", 3, {"row": {"name": "Uber"}})
        assert keys.open("c1", "r1", 3, blob) == {"row": {"name": "Uber"}}
        for label in (("c2", "r1", 3), ("c1", "r2", 3), ("c1", "r1", 2), ("c1", "r1", 4)):
            with pytest.raises(SyncCryptoError):
                keys.open(*label, blob)                                  # moved or replayed
        tampered = blob[:-1] + bytes([blob[-1] ^ 1])
        with pytest.raises(SyncCryptoError):
            keys.open("c1", "r1", 3, tampered)
        other = SyncKeys("acct-1", os.urandom(32))
        with pytest.raises(SyncCryptoError):
            other.open("c1", "r1", 3, blob)

    def test_padding_hides_size(self, made):
        keys = made[0]
        small = keys.seal("c", "r", 1, {"note": "x"})
        medium = keys.seal("c", "r", 1, {"note": os.urandom(60).hex()})
        assert len(small) == len(medium) and (len(small) - 29) % 256 == 0
        assert keys.seal("c", "r", 1, {"note": "x"}) != small           # random nonce

    def test_collection_ids(self, made):
        keys = made[0]
        other = SyncKeys("acct-2", os.urandom(32))
        assert keys.collection_id("transactions") == keys.collection_id("transactions")
        assert keys.collection_id("transactions") != other.collection_id("transactions")
        assert "transactions" not in keys.collection_id("transactions")


# --- server API -------------------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def h(request):
    harness = Harness("sqlite://" if request.param == "sqlite" else PG_URL)
    yield harness
    harness.app.state.engine.dispose()


def _b64(n: int = 64) -> str:
    return base64.b64encode(os.urandom(n)).decode()


class TestSyncApi:
    def test_keys_lifecycle(self, h):
        auth = h.auth(h.signup_verified())
        assert h.c.get("/v1/sync/keys", headers=auth).status_code == 404
        assert h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}}).json() == {"key_version": 1}
        stale = h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1, "x": 2}})
        assert stale.status_code == 409 and stale.json()["error"] == "key_conflict"
        assert h.c.put("/v1/sync/keys", headers=auth,
                       json={"envelope": {"v": 1, "x": 2}, "expected_version": 1}).json() == {"key_version": 2}
        assert h.c.get("/v1/sync/keys", headers=auth).json() == {"envelope": {"v": 1, "x": 2}, "key_version": 2}
        big = h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"x": "a" * 9000}, "expected_version": 2})
        assert big.status_code == 400

    def test_push_pull_and_conflicts(self, h):
        auth = h.auth(h.signup_verified())
        item = {"collection": "c1", "record_id": str(uuid.uuid4()), "base_version": 0, "ciphertext": _b64()}
        h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}})
        r = h.c.post("/v1/sync/push", headers=auth, json={"items": [item]}).json()
        assert r["results"][0] == {"collection": "c1", "record_id": item["record_id"], "status": "ok",
                                   "version": 1, "seq": 1} and r["cursor"] == 1
        again = h.c.post("/v1/sync/push", headers=auth, json={"items": [item]}).json()["results"][0]
        assert again["status"] == "conflict" and again["version"] == 1 and again["ciphertext"] == item["ciphertext"]
        newer = {**item, "base_version": 1, "ciphertext": _b64(80)}
        assert h.c.post("/v1/sync/push", headers=auth, json={"items": [newer]}).json()["results"][0]["version"] == 2
        pull = h.c.get("/v1/sync/pull?after=0", headers=auth).json()
        assert [(i["version"], i["seq"]) for i in pull["items"]] == [(2, 2)] and pull["cursor"] == 2
        assert h.c.get("/v1/sync/pull?after=2", headers=auth).json() == {"items": [], "cursor": 2, "more": False}

    def test_push_requires_keys(self, h):
        auth = h.auth(h.signup_verified())
        item = {"collection": "c1", "record_id": "r1", "base_version": 0, "ciphertext": _b64()}
        r = h.c.post("/v1/sync/push", headers=auth, json={"items": [item]})
        assert r.status_code == 409 and r.json()["error"] == "no_keys"

    def test_paging(self, h):
        auth = h.auth(h.signup_verified())
        h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}})
        items = [{"collection": "c", "record_id": f"r{i}", "base_version": 0, "ciphertext": _b64()} for i in range(7)]
        h.c.post("/v1/sync/push", headers=auth, json={"items": items})
        page = h.c.get("/v1/sync/pull?after=0&limit=3", headers=auth).json()
        assert len(page["items"]) == 3 and page["more"] and page["cursor"] == 3
        rest = h.c.get("/v1/sync/pull?after=3&limit=10", headers=auth).json()
        assert [i["seq"] for i in rest["items"]] == [4, 5, 6, 7] and not rest["more"]

    def test_isolation_between_accounts(self, h):
        ann = h.auth(h.signup_verified())
        bob = h.auth(h.signup_verified(email="bob@example.com"))
        h.c.put("/v1/sync/keys", headers=ann, json={"envelope": {"owner": "ann"}})
        h.c.post("/v1/sync/push", headers=ann, json={"items": [
            {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64()}]})
        assert h.c.get("/v1/sync/pull?after=0", headers=bob).json()["items"] == []
        assert h.c.get("/v1/sync/keys", headers=bob).status_code == 404
        h.c.put("/v1/sync/keys", headers=bob, json={"envelope": {"owner": "bob"}})
        r = h.c.post("/v1/sync/push", headers=bob, json={"items": [
            {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64()}]})
        assert r.json()["results"][0]["status"] == "ok"                     # same ids, separate namespaces

    def test_validation(self, h):
        auth = h.auth(h.signup_verified())
        h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}})
        bad = [{"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": "not base64!"},
               {"collection": "c/../x", "record_id": "r", "base_version": 0, "ciphertext": _b64()},
               {"collection": "c", "record_id": "r", "base_version": -1, "ciphertext": _b64()},
               {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64(4)},
               {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64(300 * 1024)}]
        for item in bad:
            assert h.c.post("/v1/sync/push", headers=auth, json={"items": [item]}).status_code in (400, 422)
        dup = {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64()}
        assert h.c.post("/v1/sync/push", headers=auth, json={"items": [dup, dup]}).status_code == 400
        many = [{**dup, "record_id": f"r{i}"} for i in range(201)]
        assert h.c.post("/v1/sync/push", headers=auth, json={"items": many}).status_code == 422

    def test_large_batches_fit(self, h):
        auth = h.auth(h.signup_verified())
        h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}})
        items = [{"collection": "c", "record_id": f"r{i}", "base_version": 0, "ciphertext": _b64(4096)}
                 for i in range(100)]                                     # ~550 KB request
        r = h.c.post("/v1/sync/push", headers=auth, json={"items": items})
        assert r.status_code == 200 and h.c.get("/v1/sync/status", headers=auth).json()["records"] == 100

    def test_reset_and_account_deletion(self, h):
        t = h.signup_verified()
        auth = h.auth(t)
        h.c.put("/v1/sync/keys", headers=auth, json={"envelope": {"v": 1}})
        h.c.post("/v1/sync/push", headers=auth, json={"items": [
            {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64()}]})
        h.clock.advance(11 * 60)
        refreshed = h.c.post("/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
        assert h.c.delete("/v1/sync", headers=h.auth(refreshed)).status_code == 403
        fresh = h.auth(h.login().json())
        assert h.c.delete("/v1/sync", headers=fresh).status_code == 204
        status = h.c.get("/v1/sync/status", headers=fresh).json()
        assert status == {"keys": False, "key_version": None, "records": 0, "bytes": 0, "cursor": 1}
        h.c.put("/v1/sync/keys", headers=fresh, json={"envelope": {"v": 1}})
        h.c.post("/v1/sync/push", headers=fresh, json={"items": [
            {"collection": "c", "record_id": "r", "base_version": 0, "ciphertext": _b64()}]})
        assert h.c.delete("/v1/account", headers=fresh).status_code == 204
        with h.app.state.engine.connect() as conn:
            counts = [conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                      for t in ("sync_keys", "sync_state", "sync_records")]
        assert counts == [0, 0, 0]


# --- engine: several devices against the real API -----------------------------------------------------------


class Device:
    def __init__(self, h: Harness, path, email="ann@example.com") -> None:
        self.db = Database(path, driver="sqlite")
        self.db.initialize()
        self.settings = SettingsStore(self.db)
        self.client = AccountClient(API, store=MemorySessionStore(), transport=AppTransport(h),
                                    clock=lambda: h.clock.mono)
        self.client.login(email, PASSWORD)
        self.sync = SyncManager(self.db, self.settings, self.client)

    def q(self, sql, *args):
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def x(self, sql, *args):
        with self.db.transaction() as conn:
            conn.execute(sql, args)

    def rows(self, table):
        skip = {"version", "sync_status", "last_synced_at", "file_path", "storage_mode"}
        return sorted(({k: v for k, v in r.items() if k not in skip} for r in self.q(f"SELECT * FROM {table}")),
                      key=lambda r: r["id"])

    def pending(self):
        return self.sync.engine().pending_count()


@pytest.fixture
def net(tmp_path):
    h = Harness("sqlite://")
    h.signup_verified()
    devices = []

    def device(email="ann@example.com"):
        d = Device(h, tmp_path / f"dev{len(devices)}.db", email)
        devices.append(d)
        return d

    yield h, device
    for d in devices:
        d.db.close()
    h.app.state.engine.dispose()


def seed(d: Device, name="Uber"):
    pid, tid, eid, vid, mid = (str(uuid.uuid4()) for _ in range(5))
    d.x("INSERT INTO platforms (id, name, kind) VALUES (?, ?, 'rideshare')", pid, name)
    d.x("INSERT INTO transactions (id, platform_id, gross_amount_minor, currency, occurred_at) "
        "VALUES (?, ?, 12345, 'USD', '2026-09-01T10:00:00.000Z')", tid, pid)
    d.x("INSERT INTO expenses (id, amount_minor, currency, occurred_at, description) "
        "VALUES (?, 4321, 'USD', '2026-09-02T10:00:00.000Z', 'Oil change at Quick Lube')", eid)
    d.x("INSERT INTO vehicles (id, nickname, vin) VALUES (?, 'Camry', '4T1BF1FK5CU123456')", vid)
    d.x("INSERT INTO mileage_logs (id, started_at, distance_m, vehicle_id, description) "
        "VALUES (?, '2026-09-03T08:00:00.000Z', 16093, ?, 'Airport run')", mid, vid)
    d.x("INSERT INTO tax_profiles (id, tax_year, country_code) VALUES (?, 2026, 'US')", str(uuid.uuid4()))
    return {"platform": pid, "transaction": tid, "expense": eid, "vehicle": vid, "trip": mid}


TABLES = ("platforms", "transactions", "expenses", "vehicles", "mileage_logs", "tax_profiles")


class TestEngine:
    def test_two_devices_converge(self, net):
        h, device = net
        a = device()
        ids = seed(a)
        recovery = a.sync.setup(SYNC_PASS)
        assert recovery and len(recovery.replace("-", "")) == 55
        report = a.sync.sync_now()
        assert report.pushed == 6 and a.pending() == 0
        b = device()
        assert b.sync.setup(SYNC_PASS) is None                            # second device: unlock, no new key
        report = b.sync.sync_now()
        assert report.applied == 6 and report.pushed == 0
        for t in TABLES:
            assert a.rows(t) == b.rows(t), t
        assert b.pending() == 0 and b.sync.sync_now().pushed == 0        # no echo
        b.x("UPDATE transactions SET gross_amount_minor = 20000 WHERE id = ?", ids["transaction"])
        assert b.sync.sync_now().pushed == 1
        a.sync.sync_now()
        assert a.q("SELECT gross_amount_minor FROM transactions")[0]["gross_amount_minor"] == 20000
        assert a.pending() == 0

    def test_concurrent_edits_newest_wins_everywhere(self, net):
        h, device = net
        a, b = device(), device()
        ids = seed(a)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        b.sync.setup(SYNC_PASS)
        b.sync.sync_now()
        a.x("UPDATE expenses SET description = 'from A', updated_at = '2026-09-20T10:00:00.000Z', "
            "sync_status = 'pending' WHERE id = ?", ids["expense"])
        b.x("UPDATE expenses SET description = 'from B', updated_at = '2026-09-20T11:00:00.000Z', "
            "sync_status = 'pending' WHERE id = ?", ids["expense"])
        a.sync.sync_now()                                                 # A pushes first
        report = b.sync.sync_now()                                        # B's push conflicts; B is newer → wins
        assert report.conflicts >= 1
        a.sync.sync_now()
        assert {d.q("SELECT description FROM expenses")[0]["description"] for d in (a, b)} == {"from B"}
        kept = b.q("SELECT kept, local_json, remote_json FROM sync_conflicts")
        assert kept and kept[0]["kept"] == "local" and "from A" in kept[0]["remote_json"]
        assert a.pending() == b.pending() == 0

    def test_soft_deletes_propagate(self, net):
        h, device = net
        a, b = device(), device()
        ids = seed(a)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        b.sync.setup(SYNC_PASS)
        b.sync.sync_now()
        b.x("UPDATE mileage_logs SET deleted_at = '2026-09-21T00:00:00.000Z' WHERE id = ?", ids["trip"])
        b.sync.sync_now()
        a.sync.sync_now()
        assert a.q("SELECT deleted_at FROM mileage_logs")[0]["deleted_at"] == "2026-09-21T00:00:00.000Z"

    def test_child_can_arrive_before_parent(self, net):
        h, device = net
        a = device()
        ids = seed(a)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        a.x("UPDATE platforms SET name = 'Uber Eats' WHERE id = ?", ids["platform"])   # parent now after child
        a.sync.sync_now()
        c = device()
        c.sync.setup(SYNC_PASS)
        c.sync.sync_now()                                                 # deferred FKs: no error
        assert c.q("SELECT name FROM platforms")[0]["name"] == "Uber Eats"
        assert c.q("SELECT platform_id FROM transactions")[0]["platform_id"] == ids["platform"]

    def test_duplicates_created_offline_merge_to_one(self, net):
        """Both devices created 'Uber' (and its earnings) before sync was turned on."""
        h, device = net
        a, b = device(), device()
        ids_a, ids_b = seed(a, "Uber"), seed(b, "uber")
        a.sync.setup(SYNC_PASS)
        b.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        b.sync.sync_now()
        a.sync.sync_now()
        b.sync.sync_now()
        survivor = min(ids_a["platform"], ids_b["platform"])
        for d in (a, b):
            live = d.q("SELECT id FROM platforms WHERE deleted_at IS NULL")
            assert [r["id"] for r in live] == [survivor]
            assert {r["platform_id"] for r in d.q("SELECT platform_id FROM transactions")} == {survivor}
            assert len(d.q("SELECT id FROM vehicles WHERE deleted_at IS NULL")) == 1       # same VIN merged
            assert len(d.q("SELECT id FROM tax_profiles WHERE deleted_at IS NULL")) == 1
            assert d.pending() == 0
        for t in TABLES:
            assert a.rows(t) == b.rows(t), t
        assert a.q("SELECT count(*) AS n FROM sync_conflicts")[0]["n"] + \
            b.q("SELECT count(*) AS n FROM sync_conflicts")[0]["n"] >= 3

    def test_receipt_paths_stay_local(self, net):
        h, device = net
        a = device()
        ids = seed(a)
        a.x("INSERT INTO receipts (id, expense_id, file_path, storage_mode, mime_type, size_bytes, sha256) "
            "VALUES (?, ?, '/home/ann/receipts/lube.jpg', 'managed', 'image/jpeg', 2048, ?)",
            str(uuid.uuid4()), ids["expense"], "ab" * 32)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        b = device()
        b.sync.setup(SYNC_PASS)
        b.sync.sync_now()
        r = b.q("SELECT file_path, storage_mode, sha256 FROM receipts")[0]
        assert r == {"file_path": f"remote:{'ab' * 32}", "storage_mode": "linked", "sha256": "ab" * 32}

    def test_wrong_passphrase_recovery_and_change(self, net):
        h, device = net
        a = device()
        seed(a)
        recovery = a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        b = device()
        with pytest.raises(WrongPassphrase):
            b.sync.setup("guessing wrong passphrase")
        assert not b.sync.enabled
        b.sync.recover(recovery, "brand new sync passphrase")
        assert b.sync.sync_now().applied == 6
        c = device()
        with pytest.raises(WrongPassphrase):
            c.sync.setup(SYNC_PASS)                                     # old passphrase replaced
        c.sync.setup("brand new sync passphrase")
        a.sync.change_passphrase("third passphrase ever")
        assert a.sync.sync_now().pushed == 0                             # A keeps its key; data unaffected
        d = device()
        d.sync.setup("third passphrase ever")
        assert d.sync.sync_now().applied == 6

    def test_new_recovery_key_replaces_old(self, net):
        h, device = net
        a = device()
        old = a.sync.setup(SYNC_PASS)
        new = a.sync.new_recovery_key()
        b = device()
        with pytest.raises(WrongPassphrase):
            b.sync.recover(old, "another passphrase 1")
        b.sync.recover(new, "another passphrase 1")

    def test_server_sees_no_plaintext(self, net):
        h, device = net
        a = device()
        seed(a)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        with h.app.state.engine.connect() as conn:
            dump = []
            for table in ("sync_keys", "sync_records", "sync_state"):
                for row in conn.execute(text(f"SELECT * FROM {table}")):
                    dump.extend(v.decode("latin-1") if isinstance(v, bytes) else str(v) for v in row)
        blob = "\n".join(dump)
        for secret in ("Uber", "12345", "4321", "Quick Lube", "4T1BF1FK5CU123456", "Camry", "Airport",
                       "16093", "transactions", "platforms", "mileage", SYNC_PASS, "rideshare"):
            assert secret not in blob, secret

    def test_tampered_or_replayed_records_stop_sync(self, net):
        h, device = net
        a = device()
        ids = seed(a)
        a.sync.setup(SYNC_PASS)
        a.sync.sync_now()
        a.x("UPDATE vehicles SET nickname = 'Prius' WHERE id = ?", ids["vehicle"])
        a.sync.sync_now()
        with h.app.state.engine.begin() as conn:                          # a hostile server alters a stored record
            rows = conn.execute(text("SELECT collection, record_id, version, ciphertext FROM sync_records")).all()
            for col, rid, ver, blob in rows:
                if ver == 2:
                    conn.execute(text("UPDATE sync_records SET ciphertext = :b WHERE collection = :c AND record_id = :r"),
                                 {"b": bytes(blob[:-1]) + bytes([blob[-1] ^ 1]), "c": col, "r": rid})
        b = device()
        b.sync.setup(SYNC_PASS)
        with pytest.raises(SyncError, match="decryption"):
            b.sync.sync_now()
        assert b.q("SELECT count(*) AS n FROM platforms")[0]["n"] == 0    # nothing half-applied
        assert b.q("SELECT value FROM sync_meta WHERE key LIKE 'pull_cursor:%'") == []

    def test_keys_are_bound_to_the_account(self, net):
        h, device = net
        h.signup_verified(email="bob@example.com")
        a = device()
        a.sync.setup(SYNC_PASS)
        a.client.logout()
        a.client.login("bob@example.com", PASSWORD)
        with pytest.raises(SyncError, match="different account"):
            a.sync.sync_now()
        a.sync.disable()
        assert a.sync.setup(SYNC_PASS)                                    # bob's own key

    def test_unknown_collections_are_skipped(self, net):
        h, device = net
        a = device()
        a.sync.setup(SYNC_PASS)
        a.client.api("POST", "/v1/sync/push", {"items": [
            {"collection": "f" * 32, "record_id": str(uuid.uuid4()), "base_version": 0, "ciphertext": _b64(300)}]})
        assert a.sync.sync_now().unknown == 1


class TestSchemaCoverage:
    def test_local_only_tables_never_sync(self, tmp_path):
        db = Database(tmp_path / "x.db", driver="sqlite")
        db.initialize()
        with db.connection() as conn:
            tables = synced_tables(conn)
        assert not set(tables) & LOCAL_ONLY_TABLES
        assert "trip_zones" not in tables and "device_settings" not in tables
        assert tables.index("platforms") < tables.index("transactions") < tables.index("health_premium_entries")
        db.close()

    def test_every_unique_index_has_a_merge_rule(self, tmp_path):
        """A future migration adding a unique index to a synced table must teach the engine about it."""
        db = Database(tmp_path / "x.db", driver="sqlite")
        db.initialize()
        with db.connection() as conn:
            unique = {(t, r[1]) for t in synced_tables(conn) for r in conn.execute(f'PRAGMA index_list("{t}")')
                      if r[2] and r[3] == "c"}
        covered = {(t, k.index) for t, keys in NATURAL_KEYS.items() for k in keys}
        assert unique == covered
        db.close()

    def test_engine_needs_keys(self, net):
        h, device = net
        a = device()
        with pytest.raises(SyncError, match="off"):
            a.sync.engine()
        assert isinstance(SyncEngine, type)
