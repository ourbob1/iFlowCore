"""
Demand radar view-model and offline-first feed service.

Feeds (each cached in `demand_feed_cache` inside the encrypted DB):

  * iCalendar URLs from Settings (team/stadium/arena calendars) — 6 h TTL
  * Ticketmaster events within the radar radius for the next 24 h — 1 h TTL
  * OpenSky arrivals for the chosen airport. OpenSky data is historical, so
    the radar projects *the same weekday last week* forward seven days as a
    pattern forecast (labelled as such) — 24 h TTL

Refresh never blocks the view on the network: a failed fetch falls back to
the last cached payload and the feed is marked stale. With no network at all
the radar still ranks cached events against the current time and position.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from core.config import UnitSystem
from core.device_settings import DeviceSettings
from core.fleet.demand import (
    AIRPORTS,
    DemandRadar,
    FlightArrival,
    OpenSkyClient,
    TicketmasterClient,
    VenueEvent,
    airport_windows,
    event_windows,
    parse_ics,
)
from core.fleet.geo import GeoPoint, geohash
from core.fleet.http import ProviderError, Transport, default_transport, send_with_retry
from core.timeutil import parse_iso, to_iso_utc
from ui.format import distance as fmt_distance
from ui.format import relative_minutes

ICS_TTL = timedelta(hours=6)
TICKETMASTER_TTL = timedelta(hours=1)
OPENSKY_TTL = timedelta(hours=24)
LOOKAHEAD = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class FeedCache:
    def __init__(self, db: Any) -> None:
        self.db = db

    def get(self, key: str, now: datetime) -> tuple[Any, datetime, bool] | None:
        """(payload, fetched_at, fresh) or None."""
        with self.db.connection() as conn:
            row = conn.execute("SELECT payload_json, fetched_at, expires_at FROM demand_feed_cache WHERE source_key = ?",
                               (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"]), parse_iso(row["fetched_at"]), parse_iso(row["expires_at"]) > now

    def put(self, key: str, payload: Any, *, now: datetime, ttl: timedelta) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO demand_feed_cache (source_key, payload_json, fetched_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (source_key) DO UPDATE SET payload_json = excluded.payload_json, "
                "fetched_at = excluded.fetched_at, expires_at = excluded.expires_at",
                (key, json.dumps(payload), to_iso_utc(now), to_iso_utc(now + ttl)),
            )


def event_to_json(e: VenueEvent) -> dict[str, Any]:
    return {
        "id": e.id, "name": e.name, "starts_at": to_iso_utc(e.starts_at),
        "ends_at": to_iso_utc(e.ends_at) if e.ends_at else None, "venue": e.venue,
        "location": [e.location.lat, e.location.lng] if e.location else None, "category": e.category,
        "attendance_estimate": e.attendance_estimate, "all_day": e.all_day, "source": e.source,
    }


def event_from_json(d: dict[str, Any]) -> VenueEvent:
    return VenueEvent(
        id=d["id"], name=d["name"], starts_at=parse_iso(d["starts_at"]),
        ends_at=parse_iso(d["ends_at"]) if d.get("ends_at") else None, venue=d.get("venue"),
        location=GeoPoint(*d["location"]) if d.get("location") else None, category=d.get("category"),
        attendance_estimate=d.get("attendance_estimate"), all_day=bool(d.get("all_day")), source=d.get("source", "cache"),
    )


def arrival_to_json(a: FlightArrival) -> dict[str, Any]:
    return {"airport": a.airport_icao, "at": to_iso_utc(a.arrives_at), "flight": a.flight, "origin": a.origin,
            "status": a.status, "seats": a.seats_estimate, "source": a.source}


def arrival_from_json(d: dict[str, Any]) -> FlightArrival:
    return FlightArrival(d["airport"], parse_iso(d["at"]), d.get("flight"), d.get("origin"), d.get("status", "landed"),
                         d.get("seats"), d.get("source", "cache"))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class FeedStatus:
    name: str
    ok: bool
    stale: bool
    count: int
    fetched_at: datetime | None
    error: str | None = None


@dataclass
class FeedSnapshot:
    events: list[VenueEvent] = field(default_factory=list)
    arrivals: list[FlightArrival] = field(default_factory=list)
    feeds: list[FeedStatus] = field(default_factory=list)

    @property
    def offline(self) -> bool:
        """True when a real fetch failed (not merely pending) for every stale feed."""
        failed = [f for f in self.feeds if not f.ok]
        return bool(failed) and len(failed) == len([f for f in self.feeds if f.stale])


def normalize_feed_url(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        raise ValueError("Calendar feeds must use https:// (or webcal://)")
    return url


class RadarService:
    def __init__(
        self,
        db: Any,
        settings: DeviceSettings,
        *,
        transport: Transport | None = None,
        ticketmaster: TicketmasterClient | None = None,
        opensky: OpenSkyClient | None = None,
        sleep: Any = None,
        failures: dict[str, str] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.cache = FeedCache(db)
        self.transport = transport or default_transport()
        self.ticketmaster = ticketmaster
        self.opensky = opensky
        self._sleep = sleep
        self._network = True
        self.needs_network = False
        # Last network error per feed; share one dict across instances so a
        # cache-only render still knows the most recent fetch failed.
        self.failures = failures if failures is not None else {}

    @property
    def airport(self):
        return AIRPORTS.get(self.settings.radar_airport_icao.upper())

    @property
    def center(self) -> GeoPoint | None:
        c = self.settings.radar_center
        if c:
            return GeoPoint(c["lat"], c["lng"])
        return self.airport.location if self.airport else None

    def _cached_fetch(self, key: str, name: str, ttl: timedelta, now: datetime, force: bool,
                      fetch: Callable[[], Any]) -> tuple[Any, FeedStatus]:
        cached = self.cache.get(key, now)
        if cached and cached[2] and not force:
            payload, fetched_at, _ = cached
            return payload, FeedStatus(name, True, False, len(payload), fetched_at)
        if not self._network:
            # Cache-only pass (instant render); caller schedules a network refresh.
            self.needs_network = True
            error = self.failures.get(key)
            ok = error is None
            if cached:
                return cached[0], FeedStatus(name, ok, True, len(cached[0]), cached[1], error or "refresh pending")
            return [], FeedStatus(name, ok, True, 0, None, error or "refresh pending")
        try:
            payload = fetch()
        except (ProviderError, ValueError, KeyError, OSError) as exc:
            self.failures[key] = str(exc)
            if cached:
                return cached[0], FeedStatus(name, False, True, len(cached[0]), cached[1], str(exc))
            return [], FeedStatus(name, False, True, 0, None, str(exc))
        self.cache.put(key, payload, now=now, ttl=ttl)
        self.failures.pop(key, None)
        return payload, FeedStatus(name, True, False, len(payload), now)

    def refresh(self, *, now: datetime, force: bool = False, network: bool = True) -> FeedSnapshot:
        """Load all feeds. `network=False` answers from the cache only and sets
        `needs_network` if anything is missing or expired."""
        self._network = network
        self.needs_network = False
        snap = FeedSnapshot()
        tz = self.settings.timezone

        for feed in self.settings.ics_feeds:
            name, url = feed.get("name") or "Calendar", feed.get("url", "")

            def fetch_ics(url: str = url, name: str = name) -> list[dict[str, Any]]:
                resp = send_with_retry(self.transport, "GET", normalize_feed_url(url), **self._kw())
                if resp.status != 200:
                    raise ProviderError(f"{name}: HTTP {resp.status}")
                parsed = parse_ics(resp.body.decode("utf-8", "replace"), default_tz=tz, source=f"ics:{name}")
                return [event_to_json(e) for e in parsed.events]

            payload, status = self._cached_fetch(f"ics:{url}", name, ICS_TTL, now, force, fetch_ics)
            snap.events += [event_from_json(d) for d in payload]
            snap.feeds.append(status)

        center = self.center
        if self.ticketmaster and center:
            key = f"tm:{geohash(center, 5)}:{self.settings.radar_radius_miles}:{now:%Y-%m-%dT%H}"

            def fetch_tm() -> list[dict[str, Any]]:
                events = self.ticketmaster.search_events(center, self.settings.radar_radius_miles, now, now + LOOKAHEAD)
                return [event_to_json(e) for e in events]

            payload, status = self._cached_fetch(key, "Ticketmaster", TICKETMASTER_TTL, now, force, fetch_tm)
            snap.events += [event_from_json(d) for d in payload]
            snap.feeds.append(status)

        airport = self.airport
        if self.opensky and airport:
            ref_day = (now - timedelta(days=7)).date()
            key = f"opensky:{airport.icao}:{ref_day.isoformat()}"
            begin = datetime(ref_day.year, ref_day.month, ref_day.day, tzinfo=timezone.utc)

            def fetch_arrivals() -> list[dict[str, Any]]:
                return [arrival_to_json(a) for a in self.opensky.arrivals(airport.icao, begin, begin + timedelta(days=2))]

            payload, status = self._cached_fetch(key, f"{airport.iata} arrivals (last week's pattern)",
                                                 OPENSKY_TTL, now, force, fetch_arrivals)
            # Project last week's arrivals onto this week as a pattern forecast.
            for d in payload:
                a = arrival_from_json(d)
                snap.arrivals.append(FlightArrival(a.airport_icao, a.arrives_at + timedelta(days=7), a.flight, a.origin,
                                                   "scheduled", a.seats_estimate, "opensky-pattern"))
            snap.feeds.append(status)

        # De-duplicate events seen in several feeds (same name + start).
        seen: set[tuple[str, datetime]] = set()
        unique = []
        for e in sorted(snap.events, key=lambda e: e.starts_at):
            k = (e.name.casefold(), e.starts_at)
            if k not in seen:
                seen.add(k)
                unique.append(e)
        snap.events = unique
        return snap

    def _kw(self) -> dict[str, Any]:
        return {"sleep": self._sleep} if self._sleep is not None else {}


# ---------------------------------------------------------------------------
# View-model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RadarRow:
    label: str
    kind: str
    starts_in_min: int
    when: str
    distance_text: str | None
    weight: int
    score: int
    urgency: str        # now | soon | later
    size: str           # small | medium | large


@dataclass(frozen=True)
class RadarData:
    rows: tuple[RadarRow, ...]
    feeds: tuple[FeedStatus, ...]
    origin_label: str
    offline: bool
    configured: bool


def size_bucket(weight: int) -> str:
    if weight >= 1_500:
        return "large"
    return "medium" if weight >= 300 else "small"


def urgency_for(starts_in_min: int) -> str:
    if starts_in_min <= 0:
        return "now"
    return "soon" if starts_in_min <= 60 else "later"


def build_radar(snapshot: FeedSnapshot, *, now: datetime, origin: GeoPoint | None, units: UnitSystem,
                airport_icao: str | None, radar: DemandRadar | None = None, limit: int = 12,
                origin_label: str = "") -> RadarData:
    radar = radar or DemandRadar(horizon=timedelta(hours=6), max_distance_m=60_000)
    windows = [w for e in snapshot.events for w in event_windows(e)]
    airport = AIRPORTS.get((airport_icao or "").upper())
    if airport:
        windows += airport_windows(snapshot.arrivals, airport)
    rows = tuple(
        RadarRow(
            label=r.window.label, kind=r.window.kind, starts_in_min=r.starts_in_min,
            when=relative_minutes(r.starts_in_min),
            distance_text=fmt_distance(r.distance_m, units) if r.distance_m is not None else None,
            weight=r.window.weight, score=r.score, urgency=urgency_for(r.starts_in_min), size=size_bucket(r.window.weight),
        )
        for r in radar.rank(windows, now=now, origin=origin, limit=limit)
    )
    configured = bool(snapshot.feeds)
    return RadarData(rows, tuple(snapshot.feeds), origin_label, snapshot.offline, configured)
