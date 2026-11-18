"""
Event intelligence & demand radar — foundations for Phase 4 surge widgets.

Data model
----------
  FlightArrival   one arriving flight (airport, time, status, seat estimate)
  VenueEvent      one stadium/arena/theatre event (venue, start, end, attendance)
  DemandWindow    a time window at a place where ride/delivery demand is
                  expected to spike, with an integer weight (≈ people)

Sources
-------
  * iCalendar (.ics) feeds — many stadiums, arenas and teams publish them;
    parsed here with the stdlib only (`parse_ics`).
  * Ticketmaster Discovery API v2 (`TicketmasterClient`) — free key,
    5,000 calls/day, 5 req/s; searched by geohash + radius.
  * OpenSky Network (`OpenSkyClient`) — free, OAuth2 client credentials.
    Its flights-by-airport data is *historical* (batch-processed, typically
    available the next day), so it suits learning weekday/hour arrival
    patterns, not live dispatch. A live schedule provider can be plugged in
    through the `FlightFeed` protocol in Phase 4.

Signal heuristics (tunable, documented defaults)
------------------------------------------------
  * Event arrival wave: from 90 min before start until start.
  * Event egress wave: 15 min before the (estimated) end until 60 min after.
    Missing end times use a per-category typical duration.
  * Airport: arrivals bucketed by 30 min; riders reach the curb ~20–60 min
    after touchdown, so each bucket becomes a window shifted by that delay.

The radar ranks windows by weight, discounted by distance from the driver
and by how far in the future they start. All scores are integers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence

from core.fleet.geo import GeoPoint, geohash, haversine_m
from core.fleet.http import ProviderAuthError, ProviderError, Transport, default_transport, send_with_retry
from core.timeutil import utc_now, zone

SignalKind = Literal["event_arrival", "event_egress", "airport_arrivals"]


class DemandError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Airport:
    icao: str
    iata: str
    name: str
    location: GeoPoint


# Approximate terminal-area coordinates for a starter set of busy US airports.
AIRPORTS: Mapping[str, Airport] = {
    a.icao: a
    for a in (
        Airport("KJFK", "JFK", "New York JFK", GeoPoint(40.641, -73.778)),
        Airport("KLGA", "LGA", "New York LaGuardia", GeoPoint(40.777, -73.874)),
        Airport("KEWR", "EWR", "Newark Liberty", GeoPoint(40.690, -74.175)),
        Airport("KBOS", "BOS", "Boston Logan", GeoPoint(42.366, -71.010)),
        Airport("KATL", "ATL", "Atlanta Hartsfield-Jackson", GeoPoint(33.641, -84.428)),
        Airport("KMIA", "MIA", "Miami", GeoPoint(25.796, -80.287)),
        Airport("KTPA", "TPA", "Tampa", GeoPoint(27.975, -82.533)),
        Airport("KMCO", "MCO", "Orlando", GeoPoint(28.429, -81.309)),
        Airport("KFLL", "FLL", "Fort Lauderdale", GeoPoint(26.072, -80.150)),
        Airport("KORD", "ORD", "Chicago O'Hare", GeoPoint(41.974, -87.907)),
        Airport("KDFW", "DFW", "Dallas/Fort Worth", GeoPoint(32.900, -97.040)),
        Airport("KDEN", "DEN", "Denver", GeoPoint(39.856, -104.674)),
        Airport("KLAX", "LAX", "Los Angeles", GeoPoint(33.942, -118.409)),
        Airport("KSFO", "SFO", "San Francisco", GeoPoint(37.621, -122.379)),
        Airport("KSEA", "SEA", "Seattle-Tacoma", GeoPoint(47.450, -122.309)),
    )
}


@dataclass(frozen=True)
class FlightArrival:
    airport_icao: str
    arrives_at: datetime                   # aware UTC (estimated/actual touchdown)
    flight: str | None = None              # callsign / flight number
    origin: str | None = None
    status: str = "scheduled"              # scheduled | landed | cancelled | diverted | unknown
    seats_estimate: int | None = None
    source: str = "manual"


@dataclass(frozen=True)
class VenueEvent:
    id: str
    name: str
    starts_at: datetime                    # aware UTC
    ends_at: datetime | None = None
    venue: str | None = None
    location: GeoPoint | None = None
    category: str | None = None
    attendance_estimate: int | None = None
    all_day: bool = False
    source: str = "manual"


@dataclass(frozen=True)
class DemandWindow:
    kind: SignalKind
    label: str
    starts_at: datetime
    ends_at: datetime
    location: GeoPoint | None
    weight: int                            # ≈ people expected to need rides
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedWindow:
    window: DemandWindow
    distance_m: int | None
    starts_in_min: int                     # ≤ 0 means happening now
    score: int


class FlightFeed(Protocol):
    def arrivals(self, airport_icao: str, begin: datetime, end: datetime) -> list[FlightArrival]: ...


class EventFeed(Protocol):
    def search_events(self, center: GeoPoint, radius_miles: int, start: datetime, end: datetime) -> list[VenueEvent]: ...


# ---------------------------------------------------------------------------
# Signal building
# ---------------------------------------------------------------------------

TYPICAL_DURATION = {
    "sports": timedelta(hours=3),
    "music": timedelta(hours=3),
    "arts & theatre": timedelta(minutes=150),
    "family": timedelta(hours=2),
}
DEFAULT_DURATION = timedelta(hours=3)
# Fallback crowd sizes when a feed gives no attendance (deliberately modest).
DEFAULT_ATTENDANCE = {"sports": 15_000, "music": 5_000, "arts & theatre": 1_500, "family": 2_000}
DEFAULT_ATTENDANCE_OTHER = 1_000


@dataclass(frozen=True)
class EventWaveProfile:
    arrival_lead: timedelta = timedelta(minutes=90)
    egress_before_end: timedelta = timedelta(minutes=15)
    egress_after_end: timedelta = timedelta(minutes=60)
    rideshare_share_bp: int = 1_000        # share of attendees assumed to use rideshare (10%)


def event_windows(event: VenueEvent, profile: EventWaveProfile = EventWaveProfile()) -> list[DemandWindow]:
    if event.all_day:
        return []          # all-day listings (festivals, markets) have no usable waves
    category = (event.category or "").casefold()
    ends = event.ends_at or event.starts_at + TYPICAL_DURATION.get(category, DEFAULT_DURATION)
    attendance = event.attendance_estimate or DEFAULT_ATTENDANCE.get(category, DEFAULT_ATTENDANCE_OTHER)
    riders = attendance * profile.rideshare_share_bp // 10_000
    label = f"{event.name}" + (f" @ {event.venue}" if event.venue else "")
    return [
        DemandWindow("event_arrival", label, event.starts_at - profile.arrival_lead, event.starts_at,
                     event.location, riders // 2, (event.id,)),
        DemandWindow("event_egress", label, ends - profile.egress_before_end, ends + profile.egress_after_end,
                     event.location, riders, (event.id,)),
    ]


def airport_windows(
    arrivals: Iterable[FlightArrival],
    airport: Airport,
    *,
    bucket: timedelta = timedelta(minutes=30),
    curb_delay: tuple[timedelta, timedelta] = (timedelta(minutes=20), timedelta(minutes=60)),
    default_seats: int = 150,
    load_factor_bp: int = 8_000,
    rideshare_share_bp: int = 1_500,
) -> list[DemandWindow]:
    """Bucket arrivals and turn each bucket into a curbside pickup window."""
    seconds = int(bucket.total_seconds())
    if seconds <= 0:
        raise DemandError("bucket must be positive")
    groups: dict[int, list[FlightArrival]] = {}
    for a in arrivals:
        if a.airport_icao != airport.icao or a.status in ("cancelled", "diverted"):
            continue
        key = int(a.arrives_at.timestamp()) // seconds * seconds
        groups.setdefault(key, []).append(a)

    out = []
    for key in sorted(groups):
        flights = groups[key]
        start = datetime.fromtimestamp(key, tz=timezone.utc)
        seats = sum(f.seats_estimate or default_seats for f in flights)
        passengers = seats * load_factor_bp // 10_000
        out.append(DemandWindow(
            "airport_arrivals", f"{airport.iata} arrivals ({len(flights)} flights)",
            start + curb_delay[0], start + bucket + curb_delay[1], airport.location,
            passengers * rideshare_share_bp // 10_000,
            tuple(f.flight or "?" for f in flights),
        ))
    return out


class DemandRadar:
    """Rank demand windows for a driver at `origin`."""

    def __init__(self, *, half_weight_distance_m: int = 10_000, horizon: timedelta = timedelta(hours=3),
                 max_distance_m: int = 40_000) -> None:
        self.half_weight_distance_m = half_weight_distance_m
        self.horizon = horizon
        self.max_distance_m = max_distance_m

    def rank(self, windows: Iterable[DemandWindow], *, now: datetime, origin: GeoPoint | None = None,
             limit: int = 10) -> list[RankedWindow]:
        horizon_s = int(self.horizon.total_seconds())
        ranked = []
        for w in windows:
            if w.ends_at <= now:
                continue
            lead_s = int((w.starts_at - now).total_seconds())
            if lead_s > horizon_s:
                continue
            distance = round(haversine_m(origin, w.location)) if (origin and w.location) else None
            if distance is not None and distance > self.max_distance_m:
                continue
            score = w.weight
            if distance is not None:
                score = score * self.half_weight_distance_m // (self.half_weight_distance_m + distance)
            if lead_s > 0:
                score = score * (horizon_s - lead_s) // horizon_s
            ranked.append(RankedWindow(w, distance, lead_s // 60, score))
        ranked.sort(key=lambda r: (-r.score, r.starts_in_min))
        return ranked[:limit]


# ---------------------------------------------------------------------------
# iCalendar feeds
# ---------------------------------------------------------------------------


@dataclass
class IcsResult:
    events: list[VenueEvent] = field(default_factory=list)
    skipped_cancelled: int = 0
    recurring_not_expanded: int = 0
    errors: list[str] = field(default_factory=list)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        raise DemandError(f"Malformed iCalendar line: {line[:60]!r}")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _ics_datetime(value: str, params: Mapping[str, str], default_tz: str) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=zone(params.get("TZID", default_tz))).astimezone(timezone.utc), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return naive.replace(tzinfo=zone(params.get("TZID", default_tz))).astimezone(timezone.utc), False


def parse_ics(text: str, *, default_tz: str = "UTC", source: str = "ics", category: str | None = None) -> IcsResult:
    """Parse VEVENTs from an iCalendar feed (RFC 5545 subset).

    Supports line folding, TZID/UTC/floating/all-day DTSTART & DTEND,
    DURATION-less events, GEO, CATEGORIES, STATUS:CANCELLED. Recurrence rules
    are not expanded (venue feeds list each game/show as its own event); the
    first occurrence is kept and counted in `recurring_not_expanded`.
    """
    result = IcsResult()
    current: dict[str, tuple[dict[str, str], str]] | None = None
    for line in _unfold(text):
        try:
            name, params, value = _split_prop(line)
        except DemandError as exc:
            result.errors.append(str(exc))
            continue
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = {}
        elif name == "END" and value.upper() == "VEVENT" and current is not None:
            _finish_vevent(current, result, default_tz, source, category)
            current = None
        elif current is not None and name not in current:
            current[name] = (params, value)
    return result


def _finish_vevent(props: dict[str, tuple[dict[str, str], str]], result: IcsResult, default_tz: str,
                   source: str, category: str | None) -> None:
    if props.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
        result.skipped_cancelled += 1
        return
    if "DTSTART" not in props:
        result.errors.append("VEVENT without DTSTART skipped")
        return
    try:
        starts, all_day = _ics_datetime(props["DTSTART"][1], props["DTSTART"][0], default_tz)
        ends = _ics_datetime(props["DTEND"][1], props["DTEND"][0], default_tz)[0] if "DTEND" in props else None
    except (ValueError, DemandError) as exc:
        result.errors.append(f"Bad date in VEVENT: {exc}")
        return
    if "RRULE" in props:
        result.recurring_not_expanded += 1
    location = None
    if "GEO" in props:
        try:
            lat, lng = (float(x) for x in props["GEO"][1].split(";"))
            location = GeoPoint(lat, lng)
        except ValueError:
            result.errors.append("Invalid GEO ignored")
    summary = _unescape(props.get("SUMMARY", ({}, "Untitled event"))[1]).strip()
    uid = props.get("UID", ({}, ""))[1].strip() or hashlib.sha256(
        f"{summary}|{starts.isoformat()}".encode()).hexdigest()[:24]
    cats = props.get("CATEGORIES", ({}, ""))[1]
    result.events.append(VenueEvent(
        id=f"{source}:{uid}", name=summary, starts_at=starts, ends_at=ends,
        venue=_unescape(props["LOCATION"][1]).strip() if "LOCATION" in props else None,
        location=location, category=category or (_unescape(cats).split(",")[0].strip().casefold() if cats else None),
        all_day=all_day, source=source,
    ))


# ---------------------------------------------------------------------------
# Ticketmaster Discovery API v2
# ---------------------------------------------------------------------------

TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
ENV_TICKETMASTER_KEY = "IFLOW_TICKETMASTER_KEY"


class TicketmasterClient:
    """Event search around a point. The API key travels as a query parameter
    (Ticketmaster's design), so URLs from this client must never be logged."""

    MAX_DEEP_PAGING = 1000       # Ticketmaster only serves items with size*page < 1000

    def __init__(self, api_key: str, *, transport: Transport | None = None, url: str = TICKETMASTER_URL,
                 page_size: int = 100, sleep: Any = None) -> None:
        if not api_key.strip():
            raise ProviderAuthError("Ticketmaster API key is required")
        self._key = api_key
        self._transport = transport or default_transport()
        self._url = url
        self._size = max(1, min(page_size, 200))
        self._sleep = sleep

    def __repr__(self) -> str:
        return "TicketmasterClient(api_key='***')"

    def search_events(self, center: GeoPoint, radius_miles: int, start: datetime, end: datetime,
                      *, classification: str | None = None, max_pages: int = 5) -> list[VenueEvent]:
        fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        events: list[VenueEvent] = []
        page = 0
        while page < max_pages and (page + 1) * self._size <= self.MAX_DEEP_PAGING:
            params = {
                "apikey": self._key, "geoPoint": geohash(center, 7), "radius": str(radius_miles), "unit": "miles",
                "startDateTime": fmt(start), "endDateTime": fmt(end), "size": str(self._size),
                "page": str(page), "sort": "date,asc",
            }
            if classification:
                params["classificationName"] = classification
            kwargs: dict[str, Any] = {"params": params}
            if self._sleep is not None:
                kwargs["sleep"] = self._sleep
            resp = send_with_retry(self._transport, "GET", self._url, **kwargs)
            if resp.status == 401:
                raise ProviderAuthError("Ticketmaster rejected the API key")
            if resp.status != 200:
                raise ProviderError(f"Ticketmaster HTTP {resp.status}")
            payload = resp.json()
            events.extend(parse_ticketmaster(payload))
            total_pages = int((payload.get("page") or {}).get("totalPages", 0))
            page += 1
            if page >= total_pages:
                break
        return events


def parse_ticketmaster(payload: Mapping[str, Any]) -> list[VenueEvent]:
    out = []
    for e in (payload.get("_embedded") or {}).get("events", []):
        dates = e.get("dates") or {}
        status = ((dates.get("status") or {}).get("code") or "").casefold()
        if status in ("cancelled", "canceled", "postponed"):
            continue
        start = dates.get("start") or {}
        all_day = False
        if start.get("dateTime"):
            starts = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
        elif start.get("localDate"):
            tz = dates.get("timezone") or "UTC"
            if start.get("localTime") and not start.get("noSpecificTime"):
                local = datetime.fromisoformat(f"{start['localDate']}T{start['localTime']}")
            else:
                local, all_day = datetime.fromisoformat(start["localDate"]), True
            starts = local.replace(tzinfo=zone(tz)).astimezone(timezone.utc)
        else:
            continue
        venue = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
        loc = venue.get("location") or {}
        point = None
        if loc.get("latitude") and loc.get("longitude"):
            try:
                point = GeoPoint(float(loc["latitude"]), float(loc["longitude"]))
            except ValueError:
                point = None
        seg = ((e.get("classifications") or [{}])[0].get("segment") or {}).get("name")
        out.append(VenueEvent(
            id=f"ticketmaster:{e.get('id')}", name=e.get("name") or "Event", starts_at=starts,
            venue=venue.get("name"), location=point, category=seg.casefold() if seg else None,
            all_day=all_day, source="ticketmaster",
        ))
    return out


# ---------------------------------------------------------------------------
# OpenSky Network (historical arrivals)
# ---------------------------------------------------------------------------

OPENSKY_API = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
ENV_OPENSKY_ID = "IFLOW_OPENSKY_CLIENT_ID"
ENV_OPENSKY_SECRET = "IFLOW_OPENSKY_CLIENT_SECRET"


class OpenSkyClient:
    """Arrivals by airport from OpenSky (OAuth2 client-credentials).

    Requests are split into ≤ 24 h chunks to stay inside the API's interval
    limit. OpenSky reports ICAO 24-bit addresses and callsigns, not seat
    counts, so `seats_estimate` is left empty (the signal builder applies a
    default).
    """

    CHUNK = timedelta(hours=24)

    def __init__(self, client_id: str, client_secret: str, *, transport: Transport | None = None,
                 api_url: str = OPENSKY_API, token_url: str = OPENSKY_TOKEN_URL,
                 clock: Callable[[], datetime] = utc_now, sleep: Any = None) -> None:
        if not client_id or not client_secret:
            raise ProviderAuthError("OpenSky client_id and client_secret are required")
        self._id, self._secret = client_id, client_secret
        self._transport = transport or default_transport()
        self._api, self._token_url = api_url, token_url
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    def __repr__(self) -> str:
        return f"OpenSkyClient(client_id={self._id!r}, client_secret='***')"

    def _kwargs(self, **extra: Any) -> dict[str, Any]:
        if self._sleep is not None:
            extra["sleep"] = self._sleep
        return extra

    def _bearer(self) -> str:
        now = self._clock()
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token
        resp = send_with_retry(self._transport, "POST", self._token_url, **self._kwargs(form={
            "grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret}))
        if resp.status in (400, 401, 403):
            raise ProviderAuthError("OpenSky rejected the client credentials")
        if resp.status != 200:
            raise ProviderError(f"OpenSky token HTTP {resp.status}")
        body = resp.json()
        self._token = body["access_token"]
        # Refresh a minute early to avoid using a token as it expires.
        self._token_expiry = now + timedelta(seconds=max(60, int(body.get("expires_in", 300))) - 60)
        return self._token

    def arrivals(self, airport_icao: str, begin: datetime, end: datetime) -> list[FlightArrival]:
        if end <= begin:
            raise DemandError("end must be after begin")
        airport_icao = airport_icao.upper()
        out: list[FlightArrival] = []
        cursor = begin
        while cursor < end:
            chunk_end = min(cursor + self.CHUNK, end)
            resp = send_with_retry(self._transport, "GET", f"{self._api}/flights/arrival", **self._kwargs(
                params={"airport": airport_icao, "begin": str(int(cursor.timestamp())), "end": str(int(chunk_end.timestamp()))},
                headers={"Authorization": f"Bearer {self._bearer()}"},
            ))
            if resp.status == 401:
                self._token = None
                raise ProviderAuthError("OpenSky token rejected")
            if resp.status == 404:        # OpenSky answers 404 when no flights match
                cursor = chunk_end
                continue
            if resp.status != 200:
                raise ProviderError(f"OpenSky HTTP {resp.status}")
            for f in resp.json() or []:
                seen = f.get("lastSeen")
                if not seen:
                    continue
                out.append(FlightArrival(
                    airport_icao=airport_icao,
                    arrives_at=datetime.fromtimestamp(int(seen), tz=timezone.utc),
                    flight=(f.get("callsign") or "").strip() or None,
                    origin=f.get("estDepartureAirport"),
                    status="landed", source="opensky",
                ))
            cursor = chunk_end
        return out


def arrival_profile(arrivals: Sequence[FlightArrival], *, tz: str, bucket_minutes: int = 60) -> dict[tuple[int, int], int]:
    """Historical pattern: (weekday 0=Mon, local bucket start minute) → arrivals
    per occurrence of that weekday. Feed several weeks of OpenSky data in to
    learn when an airport's curb gets busy."""
    if bucket_minutes <= 0 or 1440 % bucket_minutes:
        raise DemandError("bucket_minutes must divide a day evenly")
    counts: dict[tuple[int, int], int] = {}
    days: dict[int, set[date]] = {}
    for a in arrivals:
        local = a.arrives_at.astimezone(zone(tz))
        minute = (local.hour * 60 + local.minute) // bucket_minutes * bucket_minutes
        key = (local.weekday(), minute)
        counts[key] = counts.get(key, 0) + 1
        days.setdefault(local.weekday(), set()).add(local.date())
    # Average per occurrence of that weekday, rounded half-up.
    return {k: (2 * v + len(days[k[0]])) // (2 * len(days[k[0]])) for k, v in counts.items()}
