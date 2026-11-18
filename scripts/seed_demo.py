"""
Fill a *development* database with realistic demo data (Tampa-area driver):
timed shifts with per-trip earnings (airport mornings, Friday/Saturday night
peaks), coarse drop-off zones for Insights, vehicle, set-asides.

    IFLOW_DATA_DIR=/tmp/iflowcore-demo python scripts/seed_demo.py
    IFLOW_DATA_DIR=/tmp/iflowcore-demo python main.py

Refuses to touch a database that already has transactions.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import load_config  # noqa: E402
from core.device_settings import SettingsStore  # noqa: E402
from core.fleet.maintenance import record_service  # noqa: E402
from core.fleet.mileage import log_trip  # noqa: E402
from core.fleet.models import miles_to_meters  # noqa: E402
from core.fleet.vehicle import register_vehicle  # noqa: E402
from core.fleet.geo import GeoPoint  # noqa: E402
from core.intelligence.zones import PrivateZone, ZoneStore  # noqa: E402
from database.connection import Database  # noqa: E402
from ui.viewmodels.dashboard import record_set_aside  # noqa: E402
from ui.viewmodels.settings import save_tax_profile  # noqa: E402


NY = ZoneInfo("America/New_York")
HOME = GeoPoint(27.9920, -82.4610)                 # Seminole Heights: trips ending here are never zoned
# Demo drop-off areas around Tampa (only coarse cells are stored, as in the real app).
AREAS = {
    "airport": GeoPoint(27.9756, -82.5333), "downtown": GeoPoint(27.9506, -82.4572),
    "ybor": GeoPoint(27.9601, -82.4378), "westshore": GeoPoint(27.9530, -82.5060),
    "south_tampa": GeoPoint(27.9230, -82.4960), "usf": GeoPoint(28.0587, -82.4139),
}
# (weekdays, local start hour, hours, $ per trip range, drop-off areas): the patterns Insights should find
SHIFTS = (
    ((0, 1, 2, 3), 6, 3, (1800, 3200), ("airport", "airport", "westshore")),   # weekday airport mornings
    ((3,), 17, 4, (1100, 1800), ("downtown", "south_tampa", "usf")),           # Thursday evenings: average
    ((4, 5), 20, 5, (1900, 3400), ("ybor", "downtown", "ybor", "home")),       # Fri/Sat nights: the peak
    ((6,), 11, 4, (900, 1500), ("south_tampa", "westshore", "usf")),           # Sunday brunch deliveries
)


def seed_shifts(db: Database, rng: random.Random, vehicle_id: str, uber: str, dash: str, *, start: date,
                through: date) -> int:
    """Timed trips + per-trip earnings + coarse zones. Returns meters driven."""
    zones = ZoneStore(db)
    private = [PrivateZone(HOME, 250)]
    total = 0
    day = start
    while day <= through:
        for weekdays, hour, hours, (lo, hi), areas in SHIFTS:
            if day.weekday() not in weekdays or rng.random() < 0.15:          # some shifts skipped
                continue
            t = datetime(day.year, day.month, day.day, hour, tzinfo=NY) + timedelta(minutes=rng.randint(0, 20))
            end = t + timedelta(hours=hours)
            while t < end:
                minutes = rng.randint(14, 38)
                meters = miles_to_meters(rng.randint(3, 16))
                pid = dash if hour == 11 else uber
                trip = log_trip(db, started_at=t, ended_at=t + timedelta(minutes=minutes), distance_m=meters,
                                vehicle_id=vehicle_id, platform_id=pid)
                area = rng.choice(areas)
                point = HOME if area == "home" else AREAS[area]
                jitter = GeoPoint(point.lat + rng.uniform(-0.003, 0.003), point.lng + rng.uniform(-0.003, 0.003))
                zones.record(trip.id, AREAS["downtown"], jitter, private=private)
                gross = rng.randint(lo, hi)
                fee = gross * rng.randint(18, 28) // 100
                paid = t + timedelta(minutes=minutes + rng.randint(1, 6))
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO transactions (platform_id, gross_amount_minor, platform_fee_minor, currency, "
                        "base_amount_minor, occurred_at) VALUES (?, ?, ?, 'USD', ?, ?)",
                        (pid, gross, fee, gross, paid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")))
                total += meters
                t += timedelta(minutes=minutes + rng.randint(5, 25))             # wait for the next request
        day += timedelta(days=1)
    return total


def seed(db: Database, *, year: int = 2026, through: date = date(2026, 9, 26)) -> None:
    rng = random.Random(7)
    with db.connection() as conn:
        if conn.execute("SELECT count(*) FROM transactions").fetchone()[0]:
            raise SystemExit("Database already has data; not seeding.")
    with db.transaction() as conn:
        uber = "11111111-1111-4111-8111-111111111111"
        dash = "22222222-2222-4222-8222-222222222222"
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, 'Uber', 'rideshare')", (uber,))
        conn.execute("INSERT INTO platforms (id, name, kind) VALUES (?, 'DoorDash', 'delivery')", (dash,))
        day = date(year, 1, 2)
        while day <= through:
            if day.day == 3:
                conn.execute("INSERT INTO expenses (category, amount_minor, currency, occurred_at, vendor) "
                             "VALUES ('phone', 8500, 'USD', ?, 'T-Mobile')", (day.isoformat(),))
                conn.execute("INSERT INTO expenses (category, amount_minor, currency, occurred_at, vendor, business_use_bp) "
                             "VALUES ('software', 1999, 'USD', ?, 'Gridwise', 10000)", (day.isoformat(),))
            day += timedelta(days=1)

    car, _ = register_vehicle(db, nickname="Camry", year=2021, make="Toyota", model="Camry", trim="LE",
                              odometer_m=miles_to_meters(41_200), acquired_on=date(2026, 1, 1))
    odo = miles_to_meters(41_200) + seed_shifts(db, rng, car.id, uber, dash, start=date(year, 1, 2), through=through)
    with db.transaction() as conn:
        conn.execute("UPDATE vehicles SET odometer_m = ? WHERE id = ?", (odo, car.id))
    record_service(db, car.id, service="Engine oil & filter", performed_on=date(2026, 6, 1),
                   odometer_m=odo - miles_to_meters(4_700), cost_minor=7_999, vendor="Jiffy Lube")
    with db.transaction() as conn:
        conn.execute("UPDATE mileage_logs SET detection = 'auto', review_status = 'needs_review' "
                     "WHERE id IN (SELECT id FROM mileage_logs ORDER BY started_at DESC LIMIT 2)")

    save_tax_profile(db, year, filing_status="single", region_code="US-FL")
    record_set_aside(db, year=year, kind="payment", amount_minor=250_000, on=date(year, 4, 14))
    record_set_aside(db, year=year, kind="payment", amount_minor=250_000, on=date(year, 6, 13))
    record_set_aside(db, year=year, kind="reserve", amount_minor=180_000, on=date(year, 9, 1))
    SettingsStore(db).update(timezone="America/New_York", radar_airport_icao="KTPA",
                             monthly_profit_goal_minor=400_000, insights_zones_enabled=True,
                             home={"lat": HOME.lat, "lng": HOME.lng, "radius_m": 250})


if __name__ == "__main__":
    cfg = load_config()
    database = Database(cfg.db_path)
    database.initialize()
    seed(database)
    print(f"Seeded demo data into {cfg.db_path}")
