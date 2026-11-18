"""
Fleet & mileage: start/stop trips, review auto-detected trips, vehicles and
maintenance, year-to-date mileage deduction with its rate periods.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.config import UnitSystem
from core.fleet.http import ProviderError
from core.fleet.mileage import MileageError, start_trip, stop_trip
from core.fleet.vehicle import FleetError, register_vehicle
from core.fleet.vin import VinError
from core.licensing import FREE_VEHICLE_LIMIT, Feature, FeatureLocked
from core.motion.service import confirm_trip, dismiss_trip
from core.timeutil import parse_iso
from ui.components.common import card, empty_state, primary_button, status_banner, text
from ui.components.upsell import locked_dialog
from ui.format import duration, parse_distance
from ui.viewmodels.fleet import FleetData, TripRow, load_fleet
from ui.viewmodels.licensing import check_can_add_vehicle, make_decoder
from ui.views.base import ViewContext, page_column


def build(ctx: ViewContext) -> ft.Control:
    s = ctx.state
    data = load_fleet(ctx.db, today=s.today(), tz=s.tz, units=s.units, currency=s.currency)
    return render(ctx, data)


def render(ctx: ViewContext, data: FleetData) -> ft.Control:
    pal = ctx.pal
    controls: list[ft.Control] = [trip_card(ctx, data)]

    sentry = ctx.services.get("motion")
    if sentry is not None:
        controls.append(text(f"Auto-trip detection: {sentry.status_text}", pal, size=13, role="secondary"))

    if data.review:
        controls.append(card(*[review_row(ctx, t) for t in data.review], pal=pal,
                             title=f"Review auto-detected trips ({len(data.review)})", key="review-card"))

    col = {"xs": 12, "lg": 6}
    controls.append(ft.ResponsiveRow([
        vehicles_card(ctx, data, col),
        card(text(data.ytd_deduction, pal, size=26, weight=ft.FontWeight.W_700),
             text(f"{data.ytd_distance} business this year", pal, size=13, role="secondary"),
             *[text(line, pal, size=12, role="muted") for line in data.rate_lines],
             pal=pal, title="Mileage deduction", col=col, key="mileage-card"),
    ], spacing=12, run_spacing=12))

    if data.recent:
        controls.append(card(*[trip_line(t, pal) for t in data.recent], pal=pal, title="Recent trips",
                             key="recent-trips"))
    return page_column(ctx, *controls, key="view-fleet")


def trip_card(ctx: ViewContext, data: FleetData) -> ft.Container:
    pal, lay = ctx.pal, ctx.layout
    units: UnitSystem = ctx.state.units
    odo = ft.TextField(label=f"Odometer ({units.distance_unit}, optional)", keyboard_type=ft.KeyboardType.NUMBER,
                       width=260, dense=True)

    def read_odometer() -> int | None:
        try:
            return parse_distance(odo.value or "", units)
        except ValueError as exc:
            raise MileageError(f"Odometer: {exc}") from None

    def start(e: Any) -> None:
        try:
            start_trip(ctx.db, vehicle_id=data.default_vehicle_id, odometer_start_m=read_odometer(),
                       timezone=ctx.state.tz, auto_assign=data.default_vehicle_id is None)
        except (MileageError, FleetError) as exc:
            ctx.notify(str(exc))
            return
        ctx.refresh()

    def stop(e: Any) -> None:
        try:
            reading = read_odometer()
            if reading is None:
                raise MileageError("Enter the end odometer (or use GPS auto-detection)")
            result = stop_trip(ctx.db, data.active_trip.id, odometer_end_m=reading)
        except (MileageError, FleetError) as exc:
            ctx.notify(str(exc))
            return
        for w in result.warnings:
            ctx.notify(w)
        if result.maintenance_alerts:
            ctx.notify(f"Maintenance: {', '.join(a.item.service for a in result.maintenance_alerts)}")
        ctx.refresh()

    if data.active_trip:
        started = parse_iso(data.active_trip.started_at)
        elapsed = int((ctx.state.now() - started).total_seconds())
        return card(
            status_banner("good", "Trip in progress", f"Started {duration(elapsed)} ago · {data.active_trip.purpose}",
                          pal),
            ft.Row([odo, primary_button("Stop trip", stop, lay, icon="STOP_CIRCLE", key="btn-stop")],
                   wrap=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.END),
            pal=pal, title="Trip", key="trip-card")
    if not data.vehicles:
        return card(empty_state("DIRECTIONS_CAR", "Add your vehicle", "Trips and maintenance are tracked per vehicle.",
                                pal, action=("Add vehicle", lambda e: open_vehicle_dialog(ctx))),
                    pal=pal, key="trip-card")
    return card(
        ft.Row([odo, primary_button("Start trip", start, lay, icon="PLAY_CIRCLE", key="btn-start")],
               wrap=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.END),
        pal=pal, title="Trip", key="trip-card")


def review_row(ctx: ViewContext, t: TripRow) -> ft.Control:
    pal, lay = ctx.pal, ctx.layout

    def confirm(purpose: str):
        def handler(e: Any) -> None:
            confirm_trip(ctx.db, t.id, purpose=purpose)
            ctx.refresh()
        return handler

    def dismiss(e: Any) -> None:
        dismiss_trip(ctx.db, t.id)
        ctx.refresh()

    buttons = [ft.OutlinedButton(label, on_click=confirm(p), height=lay.min_target)
               for p, label in (("business", "Business"), ("personal", "Personal"), ("commute", "Commute"))]
    buttons.append(ft.TextButton("Not a trip", on_click=dismiss, height=lay.min_target))
    return ft.Column([
        text(f"{t.when} · {t.distance} · suggested {t.purpose}", pal, size=14),
        ft.Row(buttons, wrap=True, spacing=8),
    ], spacing=6, key=f"review-{t.id}")


def trip_line(t: TripRow, pal: Any) -> ft.Row:
    flags = " · auto" if t.auto else ""
    flags += " · needs review" if t.needs_review else ""
    return ft.Row([text(t.when, pal, size=13, role="secondary"), text(f"{t.distance} · {t.purpose}{flags}", pal, size=13)],
                  alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True)


def vehicles_card(ctx: ViewContext, data: FleetData, col: dict) -> ft.Container:
    pal = ctx.pal
    rows: list[ft.Control] = []
    for v in data.vehicles:
        rows.append(text(f"{v.label} · {v.odometer}", pal, size=14, weight=ft.FontWeight.W_600))
        if v.overdue:
            rows.append(status_banner("critical", "Maintenance overdue", ", ".join(v.overdue), pal))
        if v.due_soon:
            rows.append(status_banner("serious", "Maintenance due soon", ", ".join(v.due_soon), pal))
    if not rows:
        rows.append(text("No vehicles yet.", pal, size=13, role="secondary"))
    return card(*rows, pal=pal, title="Vehicles", col=col, key="vehicles-card",
                actions=[ft.IconButton(ft.Icons.ADD, tooltip="Add vehicle", on_click=lambda e: open_vehicle_dialog(ctx))])


def open_vehicle_dialog(ctx: ViewContext) -> None:
    try:
        check_can_add_vehicle(ctx.state)          # Solo Core: one vehicle; iFlow Pro: a fleet
    except FeatureLocked as exc:
        ctx.show_dialog(locked_dialog(exc.feature, ctx.navigate, ctx.close_dialog,
                                      detail=f"Solo Core includes {FREE_VEHICLE_LIMIT} vehicle."))
        return
    units: UnitSystem = ctx.state.units
    can_decode = ctx.state.allows(Feature.VIN_DECODE)
    nickname = ft.TextField(label="Nickname", autofocus=True)
    vin = ft.TextField(label="VIN (optional)", capitalization=ft.TextCapitalization.CHARACTERS, max_length=17,
                       helper=None if can_decode else "Saved and checked on this device; decoding make/model "
                                                     "needs iFlow Pro",
                       key="vehicle-vin")
    odo = ft.TextField(label=f"Current odometer ({units.distance_unit})", keyboard_type=ft.KeyboardType.NUMBER)

    def save(e: Any) -> None:
        kwargs = dict(nickname=nickname.value or "", vin=(vin.value or None),
                      odometer_m=parse_distance(odo.value or "", units) or 0)
        try:
            try:
                register_vehicle(ctx.db, decoder=make_decoder(ctx.state, ctx.services), **kwargs)  # None on Solo Core
            except ProviderError:                 # offline / provider down: keep the vehicle, skip decoding
                register_vehicle(ctx.db, decoder=None, **kwargs)
                ctx.notify("Vehicle saved. The VIN couldn't be decoded right now; details can be added later.")
        except (FleetError, VinError, ValueError) as exc:
            nickname.error = str(exc)
            nickname.update()
            return
        except Exception as exc:  # unique-constraint violations etc.
            nickname.error = f"Couldn't save: {exc}"
            nickname.update()
            return
        ctx.close_dialog()
        ctx.refresh()

    ctx.show_dialog(ft.AlertDialog(
        title=ft.Text("Add vehicle"),
        content=ft.Column([nickname, vin, odo], tight=True, spacing=12),
        actions=[ft.TextButton("Cancel", on_click=lambda e: ctx.close_dialog()), ft.FilledButton("Save", on_click=save)],
    ))
