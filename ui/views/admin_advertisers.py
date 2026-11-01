from typing import Any

import flet as ft

from ui.components.common import card, notice_banner, text
from ui.viewmodels import admin as vm
from ui.views.base import ViewContext


def advertisers_page(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    """Staged 1-click admin approval queue for pending advertisers."""
    pal = ctx.pal
    
    # Assuming vm.load fetches this from /admin/v1/advertisers
    # If not present, we default to an empty list or handle the error.
    advertisers = s.data.get("advertisers", [])
    
    if not isinstance(advertisers, list):
        advertisers = []

    pending_advertisers = [adv for adv in advertisers if adv.get("status") == "pending"]

    if not pending_advertisers:
        return ft.Column([
            card(
                text("No pending advertisers in the queue.", pal, size=14, role="secondary"),
                pal=pal, title="Approval Queue", key="adv-empty"
            )
        ], spacing=16, key="adv-page-empty")

    cards = []
    
    for adv in pending_advertisers:
        adv_id = adv.get("id", "")
        name = adv.get("name", "Unknown Advertiser")
        cpc = adv.get("cpc", 0)
        target_url = adv.get("target_url", "N/A")
        creative_url = adv.get("creative_url", "")
        
        def make_approve_handler(aid: str):
            def handler(e: Any):
                try:
                    s.client.post(f"/admin/v1/advertisers/{aid}/approve", {})
                    s.notice = f"Advertiser {aid} approved and is now live."
                    # Optimistically remove from local data
                    if "advertisers" in s.data:
                        s.data["advertisers"] = [a for a in s.data["advertisers"] if a.get("id") != aid]
                except Exception as exc:
                    s.errors["action"] = str(exc)
                redraw("advertisers")
            return handler

        def make_reject_handler(aid: str):
            def handler(e: Any):
                try:
                    s.client.post(f"/admin/v1/advertisers/{aid}/reject", {})
                    s.notice = f"Advertiser {aid} rejected."
                    if "advertisers" in s.data:
                        s.data["advertisers"] = [a for a in s.data["advertisers"] if a.get("id") != aid]
                except Exception as exc:
                    s.errors["action"] = str(exc)
                redraw("advertisers")
            return handler

        creative_preview = ft.Image(src=creative_url, height=100, fit=ft.ImageFit.CONTAIN) if creative_url else text("No creative uploaded", pal, role="muted")

        cards.append(card(
            ft.Row([
                ft.Column([
                    text(f"Advertiser: {name}", pal, size=16, weight=ft.FontWeight.W_600),
                    text(f"Target URL: {target_url}", pal, size=13, role="secondary", selectable=True),
                    text(f"Calculated CPC: {cpc} cents", pal, size=13, role="secondary"),
                ], expand=True),
                ft.Container(creative_preview, padding=10, border=ft.Border.all(1, pal.grid), border_radius=4)
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([
                ft.FilledButton("Approve & Go Live", icon=ft.Icons.CHECK_CIRCLE, on_click=make_approve_handler(adv_id), style=ft.ButtonStyle(bgcolor=ft.colors.GREEN_700)),
                ft.OutlinedButton("Reject", icon=ft.Icons.CANCEL, on_click=make_reject_handler(adv_id), style=ft.ButtonStyle(color=ft.colors.RED_700)),
            ], spacing=12),
            pal=pal, key=f"adv-card-{adv_id}"
        ))

    err = s.errors.get("action")
    
    return ft.Column([
        *([notice_banner("serious", "Action failed", err, pal, key="adv-error")] if err else []),
        *cards
    ], spacing=16, key="adv-page")
