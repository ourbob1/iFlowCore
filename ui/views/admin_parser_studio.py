"""
Admin Console → Parser Studio: define how a platform's statement export maps onto iFlow's money
roles, test it against a pasted sample, then save → publish → ship as a signed bundle.

  • Formats: every schema on the server, with its draft and published versions.
  • Definition: id, name, platform, version, timezone, currency, date formats, detection headers.
  • Test bench: paste a header line + rows, read the headers, map each role to column(s), with an
    optional regex extractor and a basis-point multiplier (e.g. -10000 flips a negative fee).
    Test Parse runs locally, so sample data never leaves this machine. The preview is integer cents.
  • Publish (confirmed) sends the signed bundle to the CDN (formats.iflowcore.com); apps check it daily and
    verify the vendor signature before installing. "Show bundle file" is the offline route.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.financial.parsers.dynamic import MONEY_ROLES, ROLES
from ui.components.common import card, notice_banner, text
from ui.viewmodels import admin as vm
from ui.viewmodels import parser_studio as ps
from ui.views.admin import section_error, table
from ui.views.base import ViewContext


def _wide(control: ft.Control) -> ft.Row:
    """Full card width (a Column child only takes its intrinsic width)."""
    control.expand = True
    return ft.Row([control])


def studio_page(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    """The whole Studio as one section (redraw("parsers") rebuilds it in place): statement formats
    (CSV/PDF files) or API integrations (live connections), picked with the tabs at the top."""
    from ui.views.admin_integrations import integrations_page

    tab = s.studio.get("tab", "formats")

    def switch(e: Any) -> None:
        s.studio["tab"] = list(e.control.selected)[0]
        redraw("parsers")

    tabs = ft.SegmentedButton(
        segments=[ft.Segment(value="formats", label=ft.Text("Statement formats"), icon=ft.Icon(ft.Icons.TABLE_CHART)),
                  ft.Segment(value="integrations", label=ft.Text("API integrations"), icon=ft.Icon(ft.Icons.LINK))],
        selected=[tab], allow_empty_selection=False, on_change=switch, key="studio-tabs")
    page = integrations_page(ctx, s, redraw) if tab == "integrations" else _formats_page(ctx, s, redraw)
    return ft.Column([tabs, page], spacing=16, key="admin-studio")


def _formats_page(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    st = ps.studio(s)
    fields: dict[str, ft.TextField | ft.Dropdown] = {}
    role_controls: dict[str, dict[str, Any]] = {}
    sample = ft.TextField(label="Sample statement (header line + a few rows)", value=st["sample"], multiline=True,
                          min_lines=4, max_lines=10, hint_text=ps.SAMPLE_HINT, text_style=ft.TextStyle(
                              font_family="monospace", size=13), key="studio-sample")

    def collect() -> None:
        """Copy what's on screen into the working copy (before every action)."""
        w = st["working"]
        for name, control in fields.items():
            w[name] = control.value if control.value is not None else ""
        try:
            w["version"] = int(str(w["version"]).strip() or "1")
        except ValueError:
            pass                                          # to_schema() reports it
        for role, c in role_controls.items():
            r = w["rules"][role]
            primary = c["column"].value or ""
            extra = [x.strip() for x in (c["extra"].value or "").split(",") if x.strip()]
            r["columns"] = [col for col in dict.fromkeys([primary, *extra]) if col]
            r["combine"] = c["combine"].value or "first"
            mode = c["pattern"].value or "whole"
            ptext = (c["label"].value if mode == "after-label" else c["custom"].value) or ""
            r["ui_pattern"] = [mode, ptext]                 # kept even while incomplete (not exported)
            r["regex"] = ps.build_pattern(mode, ptext) if ptext.strip() or mode in ("whole", "first-number") else ""
            if "amount" in c:
                amode, percent = c["amount"].value or "as-is", c["percent"].value or ""
                r["ui_amount"] = [amode, percent]
                if amode != "percent" or percent.strip():
                    try:
                        r["multiplier_bp"] = ps.bp_from(amode, percent)
                    except ValueError as exc:
                        st["error"] = f"{ROLES[role]['label']}: {exc}"
        st["sample"] = sample.value or ""

    def act(fn: Any, *sections: str) -> Any:
        def handler(e: Any) -> None:
            vm.begin(s)
            ps.clear_messages(st)
            collect()
            if st["error"]:
                redraw("parsers")
                return
            fn()
            redraw("parsers", *sections)
        return handler

    async def load_file(e: Any) -> None:
        picker = ctx.services.get("file_picker")
        if picker is None:
            st["error"] = "File picking isn't available here; paste the sample instead."
            redraw("parsers")
            return
        files = await picker.pick_files(allowed_extensions=["csv", "txt", "tsv"],
                                        dialog_title="Choose a statement to use as the sample")
        if files and files[0].path:
            open_sample(files[0].path)

    def open_sample(path: str) -> None:
        vm.begin(s)
        collect()
        ps.load_sample_file(st, path)
        redraw("parsers")

    ctx.services["studio_open_sample"] = open_sample     # tests / drag-and-drop entry point

    def open_format(schema_id: str) -> None:
        vm.begin(s)
        try:
            ps.open_schema(s, schema_id)
        except Exception as exc:                        # AdminError or a malformed stored schema
            st["error"] = getattr(exc, "message", str(exc))
        redraw("parsers")

    def new_format(e: Any) -> None:
        vm.begin(s)
        ps.start_new(s)
        redraw("parsers")

    def confirm_publish(e: Any) -> None:
        collect()
        w = st["working"]

        def go(_: Any) -> None:
            ctx.close_dialog()
            ps.publish(s)
            redraw("parsers", "audit")

        ctx.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(f"Publish {w['id'] or 'this format'} v{w['version']}?"),
            content=ft.Text("The saved draft is signed and shipped to every app the next time it updates its "
                            "statement formats. Apps refuse to go back to an older version, so fixes are new "
                            "versions. Unsaved changes on screen are not included; save the draft first."),
            actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                     ft.FilledButton("Publish", on_click=go, key="studio-publish-confirm")],
            key="studio-publish-dialog"))

    body: list[ft.Control] = []
    if st["error"]:
        body.append(notice_banner("serious", "Check this", st["error"], ctx.pal, key="studio-error"))
    if st["notice"]:
        body.append(notice_banner("good", "Done", st["notice"], ctx.pal, key="studio-notice"))
    body += [
        _formats_card(ctx, s, st, open_format, new_format, act),
        _definition_card(ctx, s, st, fields),
        _bench_card(ctx, s, st, sample, role_controls, act, confirm_publish, act(lambda: None), load_file),
    ]
    if st["preview"] is not None:
        body.append(_preview_card(ctx, st))
    if st["history"]:
        body.append(_history_card(ctx, st))
    return ft.Column(body, spacing=16, key="admin-parser-studio")


# --- cards -----------------------------------------------------------------------------------------------


def _formats_card(ctx: ViewContext, s: vm.AdminSession, st: dict[str, Any], open_format: Any, new_format: Any,
                  act: Any) -> ft.Control:
    pal = ctx.pal
    data = s.data.get("parsers") or {}
    body: list[ft.Control] = section_error(ctx, s, "parsers")
    if data and not data.get("signing"):
        body.append(notice_banner("warning", "Publishing is off", "Set RS_AI_LICENSE_SIGNING_KEY on the server to "
                                  "sign statement-format bundles. Drafts still save.", pal, key="studio-signing-off"))
    rows = data.get("schemas") or []
    if rows:
        dt = ft.DataTable(columns=[ft.DataColumn(ft.Text(c)) for c in
                                   ("Format", "Platform", "Draft", "Published", "Last change", "")],
                          column_spacing=24, key="studio-formats", rows=[
            ft.DataRow(cells=[
                ft.DataCell(ft.Text(f"{(r['draft'] or r['published'])['name']}\n{r['id']}", size=13)),
                ft.DataCell(ft.Text((r["draft"] or r["published"])["platform"], size=13)),
                ft.DataCell(ft.Text(f"v{r['draft']['version']}" if r["draft"] else "—", size=13)),
                ft.DataCell(ft.Text(f"v{r['published']['version']} · {vm.short_date(r['published']['published_at'])}"
                                    if r["published"] else "—", size=13)),
                ft.DataCell(ft.Text(f"{vm.short_date((r['draft'] or r['published'])['updated_at'])} · "
                                    f"{(r['draft'] or r['published'])['updated_by']}", size=13)),
                ft.DataCell(ft.TextButton("Open", on_click=lambda e, i=r["id"]: open_format(i),
                                          key=f"studio-open-{r['id']}")),
            ], selected=r["id"] == st["open_id"]) for r in rows])
        body.append(ft.Row([dt], scroll=ft.ScrollMode.AUTO))
    else:
        body.append(text("No statement formats yet. Start one below: paste a sample export, map the columns, "
                         "test, save, publish.", pal, size=13, role="secondary"))
    dist = data.get("distribution") or {}
    if data:
        last = dist.get("last")
        where = dist.get("public_url") or dist.get("target") or ""
        if not dist.get("configured"):
            body.append(notice_banner("warning", "No CDN target", "Published formats reach apps only through a file "
                                      "you share. Set up the CDN in My account > Publishing to the CDN (or "
                                      "RS_AI_FORMATS_BUCKET on a deployed service).", pal, key="studio-cdn-off"))
        elif last:
            body.append(notice_banner("good", "Live on the CDN",
                                      f"{last['schemas']} format(s) · sent {vm.short_date(last['issued_at'])} by "
                                      f"{last['published_by']} · {where} · sha256 {last['sha256'][:12]}", pal,
                                      key="studio-cdn-live"))
        else:
            body.append(notice_banner("warning", "Nothing on the CDN yet",
                                      f"Publishing a format sends it to {where}.", pal, key="studio-cdn-empty"))
    if st["bundle"]:
        b = st["bundle"]
        body.append(_wide(ft.TextField(value=b["bundle"], read_only=True, multiline=True, min_lines=2, max_lines=4,
                                       label=f"Signed bundle: {b['schemas']} published format(s). Apps can import "
                                             "it in Settings > Statement formats", key="studio-bundle")))
    actions = [ft.TextButton("New format", icon=ft.Icons.ADD, on_click=new_format, key="studio-new"),
               ft.TextButton("Send to CDN", icon=ft.Icons.CLOUD_UPLOAD_OUTLINED, key="studio-distribute",
                             on_click=act(lambda: ps.distribute(s), "audit"),
                             disabled=not (data.get("signing") and rows and dist.get("configured")
                                           and s.can("parsers.publish"))),
               ft.TextButton("Show bundle file", icon=ft.Icons.VERIFIED_OUTLINED, key="studio-export",
                             on_click=act(lambda: ps.export_bundle(s), "audit"),
                             disabled=not (data.get("signing") and rows))]
    return card(*body, pal=pal, title="Statement formats", actions=actions, key="studio-formats-card")


def _definition_card(ctx: ViewContext, s: vm.AdminSession, st: dict[str, Any],
                     fields: dict[str, ft.TextField | ft.Dropdown]) -> ft.Control:
    w = st["working"]

    def field(name: str, label: str, width: int, **kw: Any) -> ft.TextField:
        fields[name] = ft.TextField(label=label, value=str(w.get(name) or ""), width=width, key=f"studio-{name}",
                                    dense=True, **kw)
        return fields[name]

    fields["delimiter"] = ft.Dropdown(label="Delimiter", value=w.get("delimiter") or "", width=150, dense=True,
                                      options=[ft.DropdownOption(key=k, text=v) for k, v in ps.DELIMITERS.items()],
                                      key="studio-delimiter")
    fields["decimal_separator"] = ft.Dropdown(label="Decimal", value=w.get("decimal_separator") or ".", width=130,
                                              dense=True, key="studio-decimal_separator",
                                              options=[ft.DropdownOption(key=".", text="1,234.56"),
                                                       ft.DropdownOption(key=",", text="1.234,56")])
    title = f"Definition: {st['open_id']}" if st["open_id"] else "Definition: new format"
    return card(
        ft.Row([field("id", "Format id", 240, hint_text="uber-weekly-2026", read_only=bool(st["open_id"])),
                field("name", "Name", 260, hint_text="Uber weekly statement"),
                field("platform", "Platform", 180, hint_text="Uber"),
                field("version", "Version", 110, input_filter=ft.NumbersOnlyInputFilter())],
               wrap=True, spacing=12, run_spacing=12),
        ft.Row([field("timezone", "Statement timezone", 220, hint_text="America/Chicago (blank: the user's)"),
                field("currency", "Currency", 110), fields["delimiter"], fields["decimal_separator"]],
               wrap=True, spacing=12, run_spacing=12),
        ft.Row([field("date_formats", "Date formats (comma-separated, strftime)", 520),
                field("detect_headers", "Detect by headers (comma-separated)", 360,
                      hint_text="blank: the date column")], wrap=True, spacing=12, run_spacing=12),
        _wide(field("notes", "Notes for other admins", 900, multiline=True, max_lines=3)),
        pal=ctx.pal, title=title, key="studio-definition")


def _bench_card(ctx: ViewContext, s: vm.AdminSession, st: dict[str, Any], sample: ft.TextField,
                role_controls: dict[str, dict[str, Any]], act: Any, confirm_publish: Any, refresh: Any,
                load_file: Any) -> ft.Control:
    pal = ctx.pal
    w = st["working"]
    headers: list[str] = list(st["headers"])
    for rule in w["rules"].values():                       # keep mapped columns selectable before a sample
        for col in rule["columns"]:
            if col not in headers:
                headers.append(col)
    options = [ft.DropdownOption(key="", text="— not in this statement —")] + \
              [ft.DropdownOption(key=h, text=h) for h in headers]

    rows: list[ft.Control] = []
    for group, roles in ps.ROLE_GROUPS:
        rows.append(text(group, pal, size=13, weight=ft.FontWeight.W_600, role="secondary"))
        for role in roles:
            rule = w["rules"][role]
            cols = rule["columns"]
            mode, ptext = rule.get("ui_pattern") or ps.pattern_mode(rule["regex"])
            c: dict[str, Any] = {
                "column": ft.Dropdown(label=ROLES[role]["label"] + (" *" if ROLES[role].get("required") else ""),
                                      value=cols[0] if cols else "", options=options, width=260, dense=True,
                                      key=f"studio-col-{role}"),
                "extra": ft.TextField(label="Also columns", value=", ".join(cols[1:]), width=170, dense=True,
                                      hint_text="aliases or parts", key=f"studio-extra-{role}"),
                "combine": ft.Dropdown(label="Combine", value=rule["combine"], width=150, dense=True,
                                       options=[ft.DropdownOption(key="first", text="First found"),
                                                ft.DropdownOption(key="sum", text="Add together")],
                                       key=f"studio-combine-{role}"),
                "pattern": ft.Dropdown(label="Read the value", value=mode, width=230, dense=True, on_select=refresh,
                                       options=[ft.DropdownOption(key=k, text=v) for k, v in ps.PATTERN_MODES.items()],
                                       key=f"studio-pattern-{role}"),
                "label": ft.TextField(label="Label (e.g. Tip)", value=ptext if mode == "after-label" else "",
                                      width=190, dense=True, hint_text="e.g. Tip", visible=mode == "after-label",
                                      key=f"studio-label-{role}"),
                "custom": ft.TextField(label="Regex (one capture group)", value=ptext if mode == "custom" else "",
                                       width=260, dense=True, visible=mode == "custom", key=f"studio-regex-{role}",
                                       text_style=ft.TextStyle(font_family="monospace")),
            }
            line = [c["column"], c["extra"], c["combine"], c["pattern"], c["label"], c["custom"]]
            if role in MONEY_ROLES:
                amode, percent = rule.get("ui_amount") or ps.amount_mode(int(rule["multiplier_bp"]))
                c["amount"] = ft.Dropdown(label="Amount is", value=amode, width=260, dense=True, on_select=refresh,
                                          options=[ft.DropdownOption(key=k, text=v) for k, v in ps.AMOUNT_MODES.items()],
                                          key=f"studio-amount-{role}")
                c["percent"] = ft.TextField(label="Percent", value=percent, width=110, dense=True, suffix="%",
                                            visible=amode == "percent", key=f"studio-percent-{role}")
                line += [c["amount"], c["percent"]]
            role_controls[role] = c
            rows.append(ft.Row(line, wrap=True, spacing=10, run_spacing=10, key=f"studio-role-{role}"))
            example = ps.role_example(st, role)
            if example:
                rows.append(text(f"Example from your sample: {example}", pal, size=12, role="muted",
                                 selectable=True))

    can_publish = s.can("parsers.publish") and bool(st["open_id"])
    buttons = ft.Row([
        ft.FilledButton("Test Parse", icon=ft.Icons.PLAY_ARROW, key="studio-test", on_click=act(lambda: ps.run_test(st))),
        ft.OutlinedButton("Save draft", icon=ft.Icons.SAVE_OUTLINED, key="studio-save",
                          on_click=act(lambda: ps.save_draft(s), "audit")),
        ft.OutlinedButton("Publish…", icon=ft.Icons.PUBLISH, key="studio-publish", on_click=confirm_publish,
                          disabled=not can_publish,
                          tooltip=None if can_publish else "Save a draft first (needs the publish permission)"),
    ], wrap=True, spacing=12)
    return card(
        text("Test Parse runs on this computer: the sample is never sent to the server. Load a real statement "
             "or paste a few made-up rows, map each column, and every example below updates.", pal, size=13,
             role="secondary"),
        _wide(sample),
        ft.Row([ft.TextButton("Load sample file…", icon=ft.Icons.UPLOAD_FILE, key="studio-load-sample",
                              on_click=load_file),
                ft.TextButton("Read headers", icon=ft.Icons.VIEW_COLUMN_OUTLINED, key="studio-read-headers",
                              on_click=act(lambda: ps.read_headers(st))),
                text(f"{len(st['headers'])} columns: {', '.join(st['headers'])}" if st["headers"] else
                     "Columns appear here after reading the sample.", pal, size=12, role="muted")],
               wrap=True, spacing=8),
        ft.Divider(height=1, color=pal.grid),
        *rows,
        buttons,
        pal=pal, title="Test bench & column mapping", key="studio-bench")


def _preview_card(ctx: ViewContext, st: dict[str, Any]) -> ft.Control:
    pal = ctx.pal
    p = st["preview"]
    currency = p.rows[0].currency if p.rows else (st["working"].get("currency") or "USD")
    money = lambda v: vm.cents(v, currency)            # noqa: E731
    body: list[ft.Control] = []
    status = "good" if p.rows and not p.errors else "warning" if p.rows else "serious"
    body.append(notice_banner(status, f"{len(p.rows)} row(s) parsed",
                              f"{len(p.errors)} problem(s) · {p.skipped} skipped · net {money(p.total('net_minor'))}",
                              pal, key="studio-preview-status"))
    if p.rows:
        cols = ["Line", "Date", "Payout", "Gross", "Tips", "Bonus", "Fees", "Tolls", "Net", "Ref"]
        data = [[str(r.line), (r.occurred_at or "—").replace("T", " ")[:16], (r.paid_out_at or "—")[:10],
                 money(r.gross_minor), money(r.tips_minor), money(r.bonus_minor), money(r.fees_minor),
                 money(r.tolls_minor), money(r.net_minor), (r.reference or "—")[:18]] for r in p.rows[:50]]
        data.append(["Total", "", "", *(money(p.total(a)) for a in ("gross_minor", "tips_minor", "bonus_minor",
                                                                     "fees_minor", "tolls_minor", "net_minor")), ""])
        body.append(table(cols, data, key="studio-preview"))
        body.append(text("Net = gross + tips + bonus − fees. Tolls are pass-through and not income. Amounts are "
                         "whole cents.", pal, size=12, role="muted"))
    for line, message in p.errors[:20]:
        body.append(text(f"Line {line}: {message}", pal, size=13, role="secondary"))
    return card(*body, pal=pal, title="Test Parse preview (local)", key="studio-preview-card")


def _history_card(ctx: ViewContext, st: dict[str, Any]) -> ft.Control:
    return card(table(["Version", "Status", "Saved", "By", "Published", "By"], [
        [f"v{v['version']}", v["status"], vm.short_date(v["updated_at"]), v["updated_by"],
         vm.short_date(v["published_at"]) if v.get("published_at") else "—", v.get("published_by") or "—"]
        for v in st["history"]], key="studio-history"), pal=ctx.pal, title="Version history",
        key="studio-history-card")
