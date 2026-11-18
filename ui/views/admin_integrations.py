"""
Admin Console → Parser Studio → API integrations: build the connection form and API calls users
get for a platform (starting with Uber), check them, then save → publish → CDN.

  • Integrations: what's on the server; start from a template (Uber) or blank.
  • Schema: the JSON (fields, auth, requests, mapping), validated with the app's own model.
  • What users see: the connection form, rendered by the same component as the app's wizard,
    and the exact first-page URLs those values produce (tokens never appear in URLs).
  • Test bench: paste sample responses; the mapping runs here, locally, in integer cents.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from ui.components.common import card, notice_banner, text
from ui.components.integration_form import build_form
from ui.viewmodels import admin as vm
from ui.viewmodels import integration_studio as its
from ui.views.admin import section_error, table
from ui.views.base import ViewContext


def integrations_page(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    st = its.state(s)
    pal = ctx.pal
    editor = ft.TextField(value=st["text"], multiline=True, min_lines=16, max_lines=28, expand=True,
                          text_style=ft.TextStyle(font_family="monospace", size=12), key="api-schema")
    samples: dict[str, ft.TextField] = {}
    schema = its.parse(dict(st, error=None))           # for the preview cards; errors shown after actions
    form = build_form(schema, its.form_values(st, schema), key_prefix="api-preview") if schema else None

    def collect() -> None:
        st["text"] = editor.value or ""
        for name, field in samples.items():
            st["responses"][name] = field.value or ""
        if form is not None:
            st["values"] = {k: v for k, v in form.read().items()}

    def act(fn: Any, *sections: str) -> Any:
        def handler(e: Any) -> None:
            vm.begin(s)
            collect()
            fn()
            redraw("parsers", *sections)
        return handler

    def open_one(schema_id: str) -> None:
        vm.begin(s)
        try:
            its.open_schema(s, schema_id)
        except Exception as exc:
            st["error"] = getattr(exc, "message", str(exc))
        redraw("parsers")

    def confirm_publish(e: Any) -> None:
        collect()

        def go(_: Any) -> None:
            ctx.close_dialog()
            its.publish(s)
            redraw("parsers", "audit")

        ctx.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(f"Publish {st['open_id']}?"),
            content=ft.Text("The saved draft is signed and sent to every app with the next bundle. Users see a new "
                            "connection in Finances and the app will call this platform's API with their own "
                            "credentials. Unsaved changes on screen are not included."),
            actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                     ft.FilledButton("Publish", on_click=go, key="api-publish-confirm")],
            key="api-publish-dialog"))

    body: list[ft.Control] = []
    if st["error"]:
        body.append(notice_banner("serious", "Check this", st["error"], pal, key="api-error"))
    if st["notice"]:
        body.append(notice_banner("good", "Done", st["notice"], pal, key="api-notice"))

    # integrations on the server + templates
    data = s.data.get("integrations") or {}
    rows = data.get("integrations") or []
    listing: list[ft.Control] = section_error(ctx, s, "integrations")
    if rows:
        listing.append(table(["Integration", "Platform", "Draft", "Published", "Last change"], [
            [f"{(r['draft'] or r['published'])['name']} ({r['id']})", (r["draft"] or r["published"])["platform"],
             f"v{r['draft']['version']}" if r["draft"] else "—",
             f"v{r['published']['version']}" if r["published"] else "—",
             f"{vm.short_date((r['draft'] or r['published'])['updated_at'])} · "
             f"{(r['draft'] or r['published'])['updated_by']}"] for r in rows], key="api-list"))
        listing.append(ft.Row([ft.TextButton(f"Open {r['id']}", key=f"api-open-{r['id']}",
                                             on_click=lambda e, i=r["id"]: open_one(i)) for r in rows], wrap=True))
    else:
        listing.append(text("No API integrations yet. Start from the Uber template or a blank schema.", pal, size=13,
                            role="secondary"))
    template = ft.Dropdown(label="Template", width=240, dense=True, value=next(iter(its.TEMPLATES)), key="api-template",
                           options=[ft.DropdownOption(key=k, text=k) for k in its.TEMPLATES])
    listing.append(ft.Row([template,
                           ft.OutlinedButton("Start from template", key="api-from-template",
                                             on_click=act(lambda: its.new(s, template.value))),
                           ft.TextButton("Blank schema", key="api-blank", on_click=act(lambda: its.new(s, None)))],
                          wrap=True, spacing=12))
    body.append(card(*listing, pal=pal, title="API integrations", key="api-list-card"))

    # the schema
    status = (notice_banner("good", "Valid", its.summary(schema), pal, key="api-valid") if schema else
              notice_banner("warning", "Not valid yet", "Validate to see what's wrong.", pal, key="api-invalid"))
    can_publish = s.can("parsers.publish") and bool(st["open_id"])
    body.append(card(
        text("Fields become the user's connection form; templates like {start_date|epoch} fill the request. The "
             "token is added by the app as a header, never through a template, and requests can only go to "
             "base_url's host.", pal, size=13, role="secondary"),
        status, ft.Row([editor]),
        ft.Row([ft.OutlinedButton("Validate", icon=ft.Icons.RULE, key="api-validate", on_click=act(lambda: its.validate(st))),
                ft.TextButton("Tidy JSON", key="api-tidy", on_click=act(lambda: its.reformat(st))),
                ft.OutlinedButton("Save draft", icon=ft.Icons.SAVE_OUTLINED, key="api-save",
                                  on_click=act(lambda: its.save(s), "audit")),
                ft.OutlinedButton("Publish…", icon=ft.Icons.PUBLISH, key="api-publish", on_click=confirm_publish,
                                  disabled=not can_publish)], wrap=True, spacing=12),
        pal=pal, title=f"Schema: {st['open_id']}" if st["open_id"] else "Schema: new integration", key="api-editor"))

    if schema is not None and form is not None:
        urls = [ft.Text(f"{name}: {url}", size=12, selectable=True, font_family="monospace",
                        key=f"api-url-{name}") for name, url in st["urls"]]
        body.append(card(
            text(f"{schema.name} · {schema.description or 'no description'}", pal, size=13, weight=ft.FontWeight.W_600),
            *form.controls,
            ft.Row([ft.TextButton("Preview requests", icon=ft.Icons.LINK, key="api-preview",
                                  on_click=act(lambda: its.preview(st)))]),
            *urls, pal=pal, title="What users see", key="api-form-preview"))

        bench: list[ft.Control] = [text("Paste a real (redacted) response for each request. The mapping runs on this "
                                        "computer; nothing is sent.", pal, size=13, role="secondary")]
        for r in schema.requests:
            samples[r.name] = ft.TextField(label=f"Sample response: {r.name} ({r.role}, GET {r.path})",
                                           value=st["responses"].get(r.name, ""), multiline=True, min_lines=3,
                                           max_lines=10, expand=True, key=f"api-sample-{r.name}",
                                           text_style=ft.TextStyle(font_family="monospace", size=12))
            bench.append(ft.Row([samples[r.name]]))
        bench.append(ft.Row([ft.FilledButton("Run test", icon=ft.Icons.PLAY_ARROW, key="api-test",
                                             on_click=act(lambda: its.run_test(st)))]))
        result = st["result"]
        if result is not None:
            bench.append(notice_banner("good" if result.records and not result.errors and not result.unmapped
                                       else "warning", f"{len(result.records)} transaction(s) from {result.items} item(s)",
                                       f"{result.skipped} skipped on purpose · "
                                       + (f"unmapped categories: {', '.join(f'{k} ×{v}' for k, v in result.unmapped.items())}"
                                          if result.unmapped else "no unmapped categories")
                                       + f" · {len(result.errors)} problem(s)", pal, key="api-test-status"))
            if result.records:
                bench.append(table(["Time (UTC)", "Kind", "Amount", "Fee", "Reference", "Description"], [
                    [r.occurred_at.replace("T", " ")[:16], r.kind, vm.cents(r.gross_amount_minor, r.currency),
                     vm.cents(r.platform_fee_minor, r.currency), r.external_ref[:28], (r.description or "")[:48]]
                    for r in result.records[:50]], key="api-test-records"))
            for problem in result.errors[:10]:
                bench.append(text(problem, pal, size=12, role="secondary"))
        body.append(card(*bench, pal=pal, title="Test bench (local)", key="api-bench"))

    if st["history"]:
        body.append(card(table(["Version", "Status", "Saved", "By", "Published"], [
            [f"v{v['version']}", v["status"], vm.short_date(v["updated_at"]), v["updated_by"],
             vm.short_date(v["published_at"]) if v.get("published_at") else "—"] for v in st["history"]],
            key="api-history"), pal=pal, title="Version history", key="api-history-card"))
    return ft.Column(body, spacing=16, key="admin-integration-studio")
