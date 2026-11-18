"""Demand radar foundations: iCalendar, Ticketmaster, OpenSky, signal building, ranking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.fleet.demand import (
    AIRPORTS,
    DemandRadar,
    DemandWindow,
    EventWaveProfile,
    FlightArrival,
    OpenSkyClient,
    TicketmasterClient,
    VenueEvent,
    airport_windows,
    arrival_profile,
    event_windows,
    parse_ics,
    parse_ticketmaster,
)
from core.fleet.geo import GeoPoint, geohash
from core.fleet.http import HttpResponse, ProviderAuthError, ProviderError

from .test_fleet import FakeTransport, no_sleep, ok

UTC = timezone.utc
STADIUM = GeoPoint(40.8296, -73.9262)

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:game-1@stadium\r\nSUMMARY:Home Game vs Rivals\\, Round 1\r\n"
    "DTSTART;TZID=America/New_York:20260927T190500\r\nDTEND;TZID=America/New_York:20260927T220000\r\n"
    "LOCATION:Big Stadium\\; Gate A\r\nGEO:40.8296;-73.9262\r\nCATEGORIES:Sports,Baseball\r\n"
    "DESCRIPTION:A very long description that is folded across\r\n  multiple lines\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:concert-9\r\nSUMMARY:Concert\r\nDTSTART:20260928T000000Z\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:fest\r\nSUMMARY:Street Fair\r\nDTSTART;VALUE=DATE:20260929\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Rained out\r\nSTATUS:CANCELLED\r\nDTSTART:20260930T230000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:No start\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


# --- iCalendar ----------------------------------------------------------------------------


def test_parse_ics():
    result = parse_ics(ICS, default_tz="America/New_York", source="stadium")
    assert [e.name for e in result.events] == ["Home Game vs Rivals, Round 1", "Concert", "Street Fair"]
    game, concert, fair = result.events
    assert game.starts_at == datetime(2026, 9, 27, 23, 5, tzinfo=UTC)        # EDT → UTC
    assert game.ends_at == datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    assert game.venue == "Big Stadium; Gate A" and game.location == STADIUM
    assert game.category == "sports" and game.id == "stadium:game-1@stadium"
    assert concert.starts_at == datetime(2026, 9, 28, 0, 0, tzinfo=UTC) and concert.ends_at is None
    assert fair.all_day and fair.starts_at == datetime(2026, 9, 29, 4, 0, tzinfo=UTC)
    assert result.skipped_cancelled == 1 and result.recurring_not_expanded == 1
    assert result.errors == ["VEVENT without DTSTART skipped"]


def test_ics_bad_lines_are_reported_not_fatal():
    result = parse_ics("BEGIN:VEVENT\nnonsense line\nDTSTART:notadate\nEND:VEVENT\n")
    assert result.events == [] and len(result.errors) == 2


# --- signals -----------------------------------------------------------------------------


def event(**kw):
    base = dict(id="e1", name="Game", starts_at=datetime(2026, 9, 27, 23, 0, tzinfo=UTC), venue="Stadium",
                location=STADIUM, category="sports", attendance_estimate=40_000)
    base.update(kw)
    return VenueEvent(**base)


def test_event_windows_timing_and_weights():
    arrival, egress = event_windows(event(ends_at=datetime(2026, 9, 28, 2, 0, tzinfo=UTC)))
    assert arrival.kind == "event_arrival"
    assert arrival.starts_at == datetime(2026, 9, 27, 21, 30, tzinfo=UTC) and arrival.ends_at == event().starts_at
    assert egress.starts_at == datetime(2026, 9, 28, 1, 45, tzinfo=UTC)
    assert egress.ends_at == datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    assert (arrival.weight, egress.weight) == (2_000, 4_000)          # 10% of 40k, half on arrival
    assert egress.label == "Game @ Stadium"


def test_event_defaults_and_all_day():
    _, egress = event_windows(event(attendance_estimate=None, category="arts & theatre"))
    assert egress.starts_at == event().starts_at + timedelta(minutes=150) - timedelta(minutes=15)
    assert egress.weight == 150
    assert event_windows(event(all_day=True)) == []
    _, custom = event_windows(event(), EventWaveProfile(rideshare_share_bp=2_000))
    assert custom.weight == 8_000


def test_airport_windows_bucket_and_filter():
    jfk = AIRPORTS["KJFK"]
    t = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)
    arrivals = [
        FlightArrival("KJFK", t + timedelta(minutes=5), "DAL1", seats_estimate=200),
        FlightArrival("KJFK", t + timedelta(minutes=25), "JBU2"),
        FlightArrival("KJFK", t + timedelta(minutes=40), "AAL3", seats_estimate=300),
        FlightArrival("KJFK", t + timedelta(minutes=45), "UAL4", status="cancelled"),
        FlightArrival("KLGA", t + timedelta(minutes=10), "DAL5"),
    ]
    windows = airport_windows(arrivals, jfk)
    assert len(windows) == 2
    first, second = windows
    assert first.starts_at == t + timedelta(minutes=20) and first.ends_at == t + timedelta(minutes=90)
    assert first.source_ids == ("DAL1", "JBU2")
    assert first.weight == (200 + 150) * 8_000 // 10_000 * 1_500 // 10_000    # seats → pax → riders
    assert second.source_ids == ("AAL3",)


def test_radar_ranking():
    now = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
    near_now = DemandWindow("event_egress", "near", now - timedelta(minutes=5), now + timedelta(hours=1),
                            GeoPoint(40.83, -73.93), 1_000)
    far_now = DemandWindow("event_egress", "far", now, now + timedelta(hours=1), GeoPoint(40.60, -73.93), 1_000)
    later = DemandWindow("airport_arrivals", "later", now + timedelta(minutes=90), now + timedelta(hours=3),
                         GeoPoint(40.83, -73.93), 1_000)
    too_late = DemandWindow("event_arrival", "tomorrow", now + timedelta(hours=20), now + timedelta(hours=21), None, 9_999)
    over = DemandWindow("event_arrival", "over", now - timedelta(hours=2), now - timedelta(minutes=1), None, 9_999)
    very_far = DemandWindow("event_arrival", "boston", now, now + timedelta(hours=1), AIRPORTS["KBOS"].location, 9_999)

    ranked = DemandRadar().rank([later, far_now, near_now, too_late, over, very_far], now=now, origin=STADIUM)
    # "later" is nearby but halfway through the horizon (×0.5); "far" is live but
    # ~25 km away (×10/35.5) — so nearby-soon outranks distant-now.
    assert [r.window.label for r in ranked] == ["near", "later", "far"]
    assert ranked[0].starts_in_min == -5 and ranked[0].distance_m < 500
    assert ranked[1].starts_in_min == 90
    assert ranked[1].score < ranked[0].score and 20_000 < ranked[2].distance_m < 30_000


# --- Ticketmaster --------------------------------------------------------------------------


def tm_page(events, page, total_pages):
    return {"_embedded": {"events": events}, "page": {"number": page, "totalPages": total_pages}}


TM_EVENT = {
    "id": "G5v", "name": "Rivals at Home",
    "dates": {"start": {"dateTime": "2026-09-27T23:05:00Z", "localDate": "2026-09-27"},
              "timezone": "America/New_York", "status": {"code": "onsale"}},
    "classifications": [{"segment": {"name": "Sports"}}],
    "_embedded": {"venues": [{"name": "Big Stadium", "location": {"latitude": "40.8296", "longitude": "-73.9262"}}]},
}


def test_ticketmaster_parse():
    local_only = {**TM_EVENT, "id": "L1", "dates": {"start": {"localDate": "2026-10-01", "localTime": "19:30:00"},
                                                       "timezone": "America/Chicago"}}
    tbd = {**TM_EVENT, "id": "T1", "dates": {"start": {"localDate": "2026-10-02", "noSpecificTime": True}}}
    cancelled = {**TM_EVENT, "id": "C1", "dates": {**TM_EVENT["dates"], "status": {"code": "cancelled"}}}
    events = parse_ticketmaster(tm_page([TM_EVENT, local_only, tbd, cancelled], 0, 1))
    assert [e.id for e in events] == ["ticketmaster:G5v", "ticketmaster:L1", "ticketmaster:T1"]
    assert events[0].location == STADIUM and events[0].category == "sports" and events[0].venue == "Big Stadium"
    assert events[1].starts_at == datetime(2026, 10, 2, 0, 30, tzinfo=UTC)   # 7:30 pm CDT
    assert events[2].all_day


def test_ticketmaster_request_and_paging():
    t = FakeTransport([ok(tm_page([TM_EVENT], 0, 2)), ok(tm_page([{**TM_EVENT, "id": "B"}], 1, 2))])
    client = TicketmasterClient("secret-key", transport=t, page_size=50, sleep=no_sleep)
    start, end = datetime(2026, 9, 27, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
    events = client.search_events(STADIUM, 10, start, end, classification="Sports")
    assert len(events) == 2 and len(t.calls) == 2
    params = t.calls[0]["params"]
    assert params["geoPoint"] == geohash(STADIUM, 7) and params["radius"] == "10" and params["unit"] == "miles"
    assert params["startDateTime"] == "2026-09-27T00:00:00Z" and params["classificationName"] == "Sports"
    assert (params["size"], t.calls[1]["params"]["page"]) == ("50", "1")
    assert "secret" not in repr(client)


def test_ticketmaster_deep_paging_cap_and_errors():
    pages = [ok(tm_page([], p, 99)) for p in range(10)]
    t = FakeTransport(pages)
    TicketmasterClient("k", transport=t, page_size=200, sleep=no_sleep).search_events(
        STADIUM, 5, datetime(2026, 9, 27, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC), max_pages=10)
    assert len(t.calls) == 5                                           # size*page must stay < 1000
    with pytest.raises(ProviderAuthError):
        TicketmasterClient("k", transport=FakeTransport([HttpResponse(401, b"")]), sleep=no_sleep).search_events(
            STADIUM, 5, datetime(2026, 9, 27, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC))
    with pytest.raises(ProviderAuthError):
        TicketmasterClient(" ")


# --- OpenSky -------------------------------------------------------------------------------


def token(expires=1800):
    return ok({"access_token": "tok-1", "expires_in": expires})


def flights(*times):
    return ok([{"icao24": "abc", "callsign": f"DAL{i} ", "estDepartureAirport": "KATL", "lastSeen": ts}
               for i, ts in enumerate(times)])


def test_opensky_chunks_and_caches_token():
    begin = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    t = FakeTransport([token(), flights(int(begin.timestamp()) + 600), HttpResponse(404, b"")])
    client = OpenSkyClient("id", "s3cr3t-value", transport=t, clock=lambda: begin, sleep=no_sleep)
    result = client.arrivals("kjfk", begin, begin + timedelta(hours=36))
    assert [c["method"] for c in t.calls] == ["POST", "GET", "GET"]         # one token, two ≤24h chunks
    assert t.calls[0]["form"]["grant_type"] == "client_credentials"
    assert t.calls[1]["headers"]["Authorization"] == "Bearer tok-1"
    assert t.calls[1]["params"]["airport"] == "KJFK"
    assert int(t.calls[2]["params"]["end"]) - int(t.calls[2]["params"]["begin"]) == 12 * 3600
    assert len(result) == 1 and result[0].flight == "DAL0" and result[0].origin == "KATL"
    assert "s3cr3t-value" not in repr(client)


def test_opensky_token_refresh_and_auth_errors():
    now = [datetime(2026, 9, 20, tzinfo=UTC)]
    t = FakeTransport([token(120), flights(), token(), flights()])
    client = OpenSkyClient("id", "s", transport=t, clock=lambda: now[0], sleep=no_sleep)
    client.arrivals("KJFK", now[0], now[0] + timedelta(hours=1))
    now[0] += timedelta(seconds=90)                                    # past (120 − 60) s
    client.arrivals("KJFK", now[0], now[0] + timedelta(hours=1))
    assert [c["method"] for c in t.calls] == ["POST", "GET", "POST", "GET"]
    with pytest.raises(ProviderAuthError):
        OpenSkyClient("id", "bad", transport=FakeTransport([HttpResponse(401, b"")]), sleep=no_sleep).arrivals(
            "KJFK", now[0], now[0] + timedelta(hours=1))
    with pytest.raises(ProviderError):
        OpenSkyClient("id", "s", transport=FakeTransport([token()] + [HttpResponse(500, b"")] * 3), sleep=no_sleep,
                      clock=lambda: now[0]).arrivals("KJFK", now[0], now[0] + timedelta(hours=1))


def test_arrival_profile_averages_by_local_weekday_hour():
    # Two Mondays (EDT): 3 arrivals 17:xx on the first, 2 on the second → average 3 (2.5 rounds up).
    mondays = [datetime(2026, 9, 21, 21, 10, tzinfo=UTC), datetime(2026, 9, 28, 21, 10, tzinfo=UTC)]
    arrivals = [FlightArrival("KJFK", mondays[0] + timedelta(minutes=m)) for m in (0, 5, 10)]
    arrivals += [FlightArrival("KJFK", mondays[1] + timedelta(minutes=m)) for m in (0, 5)]
    profile = arrival_profile(arrivals, tz="America/New_York")
    assert profile == {(0, 17 * 60): 3}
    with pytest.raises(Exception):
        arrival_profile(arrivals, tz="UTC", bucket_minutes=7)
