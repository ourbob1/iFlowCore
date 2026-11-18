"""
Demand radar: upcoming crowd waves (venue events, airport arrivals) ranked by
size, proximity and urgency. Works offline from cached feeds.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.fleet.demand import AIRPORTS, ENV_OPENSKY_ID, ENV_OPENSKY_SECRET, ENV_TICKETMASTER_KEY, OpenSkyClient, \
    TicketmasterClient
from core.fleet.geo import GeoPoint
from ui.components.common import card, empty_state, status_banner, text
from ui.viewmodels.radar import RadarData, RadarRow, RadarService, build_radar
from ui.views.base import ViewContext, page_column

KIND_ICON = {"event_arrival": "STADIUM", "event_egress": "STADIUM", "airport_arrivals": "FLIGHT_LAND"}
KIND_LABEL = {"event_arrival": "Arrivals wave", "event_egress": "Let-out wave", "airport_arrivals": "Airport pickups"}
SIZE_LABEL = {"small": "Small crowd", "medium": "Medium crowd", "large": "Large crowd"}


def make_service(ctx: ViewContext) -> RadarService:
    factory = ctx.services.get("radar_service")
    if factory:
        return factory(ctx)
    s = ctx.state
    store = s.store
    tm_key = store.secret("ticketmaster_api_key", ENV_TICKETMASTER_KEY) if store else None
    os_id = store.secret("opensky_client_id", ENV_OPENSKY_ID) if store else None
    os_secret = store.secret("opensky_client_secret", ENV_OPENSKY_SECRET) if store else None
    return RadarService(
        ctx.db, s.settings, failures=ctx.services.setdefault("radar_failures", {}),
        ticketmaster=TicketmasterClient(tm_key) if tm_key else None,
        opensky=OpenSkyClient(os_id, os_secret) if (os_id and os_secret) else None,
    )


def origin_for(ctx: ViewContext, service: RadarService) -> tuple[GeoPoint | None, str]:
    live = ctx.services.get("last_position")
    if isinstance(live, GeoPoint):
        return live, "your location"
    airport = AIRPORTS.get(ctx.state.settings.radar_airport_icao.upper())
    return service.center, (f"near {airport.iata}" if airport else "radar center")


REFRESH_THROTTLE_S = 300


def build(ctx: ViewContext) -> ft.Control:
    """Render instantly from the cache, then refresh from the network in the
    background (throttled) and re-render when it finishes."""
    service = make_service(ctx)
    now = ctx.state.now()
    force = ctx.params.get("refresh") == "1"
    snapshot = service.refresh(now=now, network=False)
    last = ctx.services.get("radar_last_attempt")
    due = last is None or (now - last).total_seconds() >= REFRESH_THROTTLE_S
    runner = ctx.services.get("run_background")
    if runner and (force or (service.needs_network and due)):
        ctx.services["radar_last_attempt"] = now

        def fetch() -> None:
            make_service(ctx).refresh(now=ctx.state.now(), force=force, network=True)
            # Drop ?refresh=1 so the re-render doesn't trigger another forced fetch.
            ctx.navigate("/radar") if force else ctx.refresh()

        runner(fetch)
    origin, label = origin_for(ctx, service)
    data = build_radar(snapshot, now=now, origin=origin, units=ctx.state.units,
                       airport_icao=ctx.state.settings.radar_airport_icao, origin_label=label)
    return render(ctx, data)


def render(ctx: ViewContext, data: RadarData) -> ft.Control:
    pal, lay = ctx.pal, ctx.layout

    def refresh(e: Any) -> None:
        ctx.navigate("/radar?refresh=1")

    header = ft.Row([
        ft.Column([text("Demand radar", pal, size=22, weight=ft.FontWeight.W_700),
                   text(f"Next 6 hours · ranked from {data.origin_label}", pal, size=13, role="secondary")],
                  spacing=2, tight=True, expand=True),
        ft.IconButton(ft.Icons.REFRESH, tooltip="Refresh feeds", on_click=refresh, key="radar-refresh",
                      width=lay.min_target, height=lay.min_target),
    ])
    controls: list[ft.Control] = [header]

    if not data.configured:
        controls.append(empty_state(
            "RADAR", "Add event and flight sources",
            "Add team or venue calendars (ICS), a Ticketmaster key, or OpenSky credentials in Settings.", pal,
            action=("Open settings", lambda e: ctx.navigate("/settings")), key="radar-empty"))
        return page_column(ctx, *controls, key="view-radar")

    if data.offline:
        controls.append(status_banner("warning", "Showing saved data",
                                      "Couldn't reach the feeds; rankings use the last download.", pal,
                                      key="radar-offline"))

    if data.rows:
        cards = [row_card(r, ctx) for r in data.rows]
        controls.append(ft.ResponsiveRow(cards, spacing=12, run_spacing=12, key="radar-rows"))
    else:
        controls.append(empty_state("EVENT_BUSY", "Nothing big coming up",
                                    "No events or arrival waves in the next six hours near you.", pal))

    feed_lines = [
        ft.Row([
            ft.Icon(ft.Icons.CHECK_CIRCLE if f.ok else ft.Icons.CLOUD_OFF, size=18,
                   color=pal.text_secondary),
            text(f"{f.name} — {f.count} item(s)" + (" · saved copy" if f.stale else ""), pal, size=13,
                 role="secondary"),
        ], spacing=8)
        for f in data.feeds
    ]
    controls.append(card(*feed_lines, pal=pal, title="Sources", key="radar-sources"))
    return page_column(ctx, *controls, key="view-radar")


def row_card(r: RadarRow, ctx: ViewContext) -> ft.Container:
    pal = ctx.pal
    chips = [chip(r.when, pal, strong=r.urgency == "now"), chip(SIZE_LABEL[r.size], pal)]
    if r.distance_text:
        chips.append(chip(r.distance_text, pal))
    return card(
        ft.Row([
            ft.Icon(getattr(ft.Icons, KIND_ICON.get(r.kind, "PLACE")), color=pal.text_secondary, size=28),
            ft.Column([
                text(r.label, pal, size=15, weight=ft.FontWeight.W_600),
                text(f"{KIND_LABEL.get(r.kind, r.kind)} · ~{r.weight:,} riders", pal, size=12, role="secondary"),
            ], spacing=2, tight=True, expand=True),
        ], spacing=12),
        ft.Row(chips, spacing=6, wrap=True),
        pal=pal, col={"xs": 12, "md": 6, "xl": 4}, padding=14,
    )


def chip(label: str, pal: Any, *, strong: bool = False) -> ft.Container:
    return ft.Container(
        text(label, pal, size=12, weight=ft.FontWeight.W_600 if strong else None,
             role="primary" if strong else "secondary"),
        padding=ft.Padding.symmetric(horizontal=10, vertical=4), border=ft.Border.all(1, pal.grid),
        border_radius=ft.BorderRadius.all(16),
    )
