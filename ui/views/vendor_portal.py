from typing import Any

import flet as ft

from ui.components.common import card, notice_banner, text
from ui.theme import palette
from core.monetization.vendor_auth import process_vendor_registration


def vendor_portal_view(page: ft.Page) -> ft.Control:
    """External-facing public web view for vendor applications."""
    pal = palette("light")
    
    # Form state
    form_data = {
        "company_name": "",
        "contact_email": "",
        "industry": "general",
        "company_size": "11-50",
        "annual_revenue_tier": "1m-10m",
        "title": "",
        "verbiage": "",
        "target_url": "",
        "image_path": "",
        "turnstile_token": "mock-token-from-js" # In a real Flet web app, this would be populated via JS interop
    }

    def handle_submit(e: Any):
        # Gather values
        form_data["company_name"] = company_name.value
        form_data["contact_email"] = contact_email.value
        form_data["industry"] = industry.value
        form_data["company_size"] = company_size.value
        form_data["annual_revenue_tier"] = annual_revenue_tier.value
        form_data["title"] = ad_title.value
        form_data["verbiage"] = ad_verbiage.value
        form_data["target_url"] = target_url.value
        
        try:
            # Process registration
            process_vendor_registration(form_data)
            
            # Show success screen
            page.views.pop()
            page.views.append(
                ft.View(
                    "/vendor/success",
                    [success_screen(pal)]
                )
            )
            page.update()
        except Exception as ex:
            error_banner.content = notice_banner("serious", "Registration Failed", str(ex), pal)
            error_banner.visible = True
            page.update()

    # Form Fields
    company_name = ft.TextField(label="Company Name", width=400)
    contact_email = ft.TextField(label="Contact Email (Corporate Domain)", width=400)
    
    industry = ft.Dropdown(
        label="Industry",
        options=[
            ft.dropdown.Option("insurance", "Insurance"),
            ft.dropdown.Option("tax services", "Tax Services"),
            ft.dropdown.Option("fleet leasing", "Fleet Leasing"),
            ft.dropdown.Option("banking", "Banking"),
            ft.dropdown.Option("legal", "Legal"),
            ft.dropdown.Option("fuel & energy", "Fuel & Energy"),
            ft.dropdown.Option("maintenance", "Maintenance"),
            ft.dropdown.Option("telecom", "Telecom"),
            ft.dropdown.Option("education", "Education"),
            ft.dropdown.Option("general", "General"),
        ],
        value="general",
        width=400
    )
    
    company_size = ft.Dropdown(
        label="Company Size",
        options=[
            ft.dropdown.Option("1-10", "1-10 Employees"),
            ft.dropdown.Option("11-50", "11-50 Employees"),
            ft.dropdown.Option("51-250", "51-250 Employees"),
            ft.dropdown.Option("250+", "250+ Employees"),
        ],
        value="11-50",
        width=400
    )
    
    annual_revenue_tier = ft.Dropdown(
        label="Annual Revenue Tier",
        options=[
            ft.dropdown.Option("<1m", "Under $1M"),
            ft.dropdown.Option("1m-10m", "$1M - $10M"),
            ft.dropdown.Option("10m-50m", "$10M - $50M"),
            ft.dropdown.Option("50m+", "$50M+"),
        ],
        value="1m-10m",
        width=400
    )

    ad_title = ft.TextField(label="Ad Title", width=400)
    ad_verbiage = ft.TextField(label="Ad Verbiage (Description)", multiline=True, width=400)
    target_url = ft.TextField(label="Target URL", width=400)
    
    # File picker for image
    def on_file_picked(e: ft.FilePickerResultEvent):
        if e.files:
            form_data["image_path"] = e.files[0].name
            file_name_text.value = f"Selected: {e.files[0].name}"
            file_name_text.update()

    file_picker = ft.FilePicker(on_result=on_file_picked)
    page.overlay.append(file_picker)
    
    file_name_text = ft.Text("No file selected", color=pal.text_secondary)
    upload_btn = ft.ElevatedButton(
        "Upload Ad Image",
        icon=ft.Icons.UPLOAD_FILE,
        on_click=lambda _: file_picker.pick_files(allow_multiple=False)
    )

    error_banner = ft.Container(visible=False)

    # Turnstile placeholder (In a real web deployment, this would be injected via page.add(ft.Html(...)) or similar)
    turnstile_widget = ft.Container(
        content=ft.Text("Cloudflare Turnstile Verification Active", color=ft.colors.GREEN_700),
        padding=10,
        border=ft.Border.all(1, ft.colors.GREEN_700),
        border_radius=4,
        width=400
    )

    form_content = ft.Column([
        text("Vendor Registration", pal, size=24, weight=ft.FontWeight.W_700),
        text("Apply to become an advertiser on our platform.", pal, size=14, role="secondary"),
        error_banner,
        company_name,
        contact_email,
        industry,
        company_size,
        annual_revenue_tier,
        ft.Divider(),
        text("Ad Creative Details", pal, size=18, weight=ft.FontWeight.W_600),
        ad_title,
        ad_verbiage,
        target_url,
        ft.Row([upload_btn, file_name_text]),
        ft.Divider(),
        turnstile_widget,
        ft.FilledButton("Submit Application", on_click=handle_submit, width=400, height=45)
    ], spacing=16, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    return ft.Container(
        content=card(form_content, pal=pal, title="", key="vendor-form"),
        alignment=ft.alignment.center,
        padding=40
    )


def success_screen(pal: Any) -> ft.Control:
    """Success / Pending State Screen."""
    return ft.Container(
        content=card(
            ft.Column([
                ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, color=ft.colors.GREEN_600, size=64),
                text("Application Submitted Successfully", pal, size=24, weight=ft.FontWeight.W_700),
                text(
                    "Your application has been securely submitted and is awaiting automated compliance review "
                    "and admin activation. We will contact you at your corporate email once approved.",
                    pal, size=14, role="secondary", text_align=ft.TextAlign.CENTER
                )
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=16),
            pal=pal, title="", key="vendor-success"
        ),
        alignment=ft.alignment.center,
        padding=40
    )
