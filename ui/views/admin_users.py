"""
Admin Console → Users: console accounts, roles and access (needs users.manage).

  • Directory: every console user with role, status and last sign-in. Change a role, disable or
    re-enable, reset a password (a temporary one, shown once) or clear a lockout.
  • Add user: username + display name + role; the server generates a temporary password that the
    new user must change at first sign-in.
  • Roles & permissions: what each role can do (read-only; roles are defined on the server).

The server enforces every rule (owners only manage owners, nobody changes their own role or
disables themselves, there is always an active owner); the page just doesn't offer what would fail.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from core.admin.client import AdminError
from ui.components.common import card, notice_banner, text
from ui.viewmodels import admin as vm
from ui.views.admin import section_error
from ui.views.base import ViewContext

PERMISSION_LABELS = {
    "users.manage": "Manage console users",
    "licenses.issue": "Issue licenses",
    "licenses.read": "See licenses & revenue",
    "parsers.edit": "Edit statement formats",
    "parsers.publish": "Publish statement formats",
    "telemetry.read": "Read crash reports",
    "telemetry.triage": "Resolve & purge crash reports",
    "health.read": "See system health",
    "audit.read": "Read the audit trail",
}


def _can_manage(s: vm.AdminSession, user: dict[str, Any]) -> bool:
    return s.user.get("role") == "owner" or user["role"] != "owner"


def _is_self(s: vm.AdminSession, user: dict[str, Any]) -> bool:
    return user["id"] == s.user.get("id")


def users_page(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    return ft.Column([_temp_password(ctx, s), _directory(ctx, s, redraw), _add_user(ctx, s, redraw),
                      _roles_matrix(ctx, s)], spacing=16, key="admin-users")


def _run(s: vm.AdminSession, redraw: Any, fn: Any, notice: str | None = None) -> None:
    vm.begin(s)
    s.temp_password = None
    try:
        fn()
        s.notice = notice
    except AdminError as exc:
        s.errors["action"] = exc.message
    vm.load(s, ("users", "audit"))
    redraw("users", "audit")


def _temp_password(ctx: ViewContext, s: vm.AdminSession) -> ft.Control:
    body: list[ft.Control] = []
    if s.errors.get("action"):
        body.append(notice_banner("serious", "Not done", s.errors["action"], ctx.pal, key="users-error"))
    if s.temp_password:
        t = s.temp_password
        body.append(card(
            notice_banner("warning", "Shown once", f"Give this temporary password to {t['username']} privately. They "
                          "must choose their own at first sign-in. It isn't stored anywhere readable.", ctx.pal),
            ft.TextField(value=t["password"], read_only=True, label=f"Temporary password for {t['username']}",
                         text_style=ft.TextStyle(font_family="monospace", size=16), width=420,
                         key="users-temp-password"),
            ft.TextButton("I've handed it over: hide it", key="users-temp-hide",
                          on_click=lambda e: _hide(s, e)),
            pal=ctx.pal, key="users-temp-card"))
    return ft.Column(body, spacing=12, key="users-messages")


def _hide(s: vm.AdminSession, e: Any) -> None:
    s.temp_password = None
    control = e.control if e is not None else None
    parent = getattr(control, "parent", None)
    while parent is not None and getattr(parent, "key", None) != "users-temp-card":
        parent = getattr(parent, "parent", None)
    if parent is not None:
        parent.visible = False
        parent.update()


def _directory(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    pal = ctx.pal
    data = s.data.get("users") or {}
    roles = data.get("roles") or []
    body: list[ft.Control] = section_error(ctx, s, "users")
    rows = []
    for u in data.get("users") or []:
        manageable = _can_manage(s, u)
        me = _is_self(s, u)
        role_options = [ft.DropdownOption(key=r["id"], text=r["label"]) for r in roles
                        if r["id"] != "owner" or s.user.get("role") == "owner" or u["role"] == "owner"]

        def change_role(e: Any, u: dict[str, Any] = u) -> None:
            new = e.control.value
            if new and new != u["role"]:
                _run(s, redraw, lambda: s.client.update_user(u["id"], role=new),
                     f"{u['username']} is now {next(r['label'] for r in roles if r['id'] == new)}. "
                     "Their open sessions were signed out.")

        role = ft.Dropdown(value=u["role"], options=role_options, width=170, dense=True, on_select=change_role,
                           disabled=not manageable or me, key=f"users-role-{u['username']}",
                           tooltip="You can't change your own role" if me else None)
        status = ("Disabled" if not u["active"] else "Locked out" if u["locked"] else
                  "Must change password" if u["must_change_password"] else "Active")
        actions: list[ft.Control] = []
        if manageable:
            actions.append(ft.TextButton(
                "Reset password", key=f"users-reset-{u['username']}",
                on_click=lambda e, u=u: _reset(ctx, s, redraw, u)))
            if u["locked"]:
                actions.append(ft.TextButton("Unlock", key=f"users-unlock-{u['username']}", on_click=lambda e, u=u: _run(
                    s, redraw, lambda: s.client.unlock_user(u["id"]), f"{u['username']} can sign in again.")))
            if not me:
                label = "Disable" if u["active"] else "Enable"
                actions.append(ft.TextButton(label, key=f"users-toggle-{u['username']}",
                                             on_click=lambda e, u=u: _toggle(ctx, s, redraw, u)))
        rows.append(ft.DataRow(cells=[
            ft.DataCell(ft.Text(f"{u['display_name'] or u['username']}\n{u['username']}" + (" (you)" if me else ""),
                                size=13)),
            ft.DataCell(role),
            ft.DataCell(ft.Text(status, size=13)),
            ft.DataCell(ft.Text(vm.short_date(u["last_login_at"]) if u.get("last_login_at") else "Never", size=13)),
            ft.DataCell(ft.Text(f"{vm.short_date(u['created_at'])} · {u['created_by']}", size=13)),
            ft.DataCell(ft.Row(actions, spacing=0)),
        ]))
    if rows:
        body.append(ft.Row([ft.DataTable(columns=[ft.DataColumn(ft.Text(c)) for c in
                                                  ("User", "Role", "Status", "Last sign-in", "Created", "Actions")],
                                         rows=rows, column_spacing=20, data_row_max_height=64,
                                         key="users-table")], scroll=ft.ScrollMode.AUTO))
    return card(*body, pal=pal, title=f"Console users ({len(rows)})", key="users-directory")


def _reset(ctx: ViewContext, s: vm.AdminSession, redraw: Any, u: dict[str, Any]) -> None:
    def go(_: Any) -> None:
        ctx.close_dialog()
        vm.begin(s)
        try:
            temp = s.client.reset_password(u["id"])
            s.temp_password = {"username": u["username"], "password": temp}
        except AdminError as exc:
            s.temp_password = None
            s.errors["action"] = exc.message
        vm.load(s, ("users", "audit"))
        redraw("users", "audit")

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(f"Reset {u['username']}'s password?"),
        content=ft.Text("Their current password stops working and they're signed out everywhere. You'll get a "
                        "temporary password to hand over; they choose a new one at sign-in."),
        actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                 ft.FilledButton("Reset password", on_click=go, key="users-reset-confirm")],
        key="users-reset-dialog"))


def _toggle(ctx: ViewContext, s: vm.AdminSession, redraw: Any, u: dict[str, Any]) -> None:
    if not u["active"]:
        _run(s, redraw, lambda: s.client.update_user(u["id"], active=True), f"{u['username']} is enabled.")
        return

    def go(_: Any) -> None:
        ctx.close_dialog()
        _run(s, redraw, lambda: s.client.update_user(u["id"], active=False),
             f"{u['username']} is disabled and signed out.")

    ctx.show_dialog(ft.AlertDialog(
        modal=True, title=ft.Text(f"Disable {u['username']}?"),
        content=ft.Text("They're signed out now and can't sign in until someone enables the account again. "
                        "Their past actions stay in the audit trail."),
        actions=[ft.TextButton("Cancel", on_click=lambda _: ctx.close_dialog()),
                 ft.FilledButton("Disable", on_click=go, key="users-disable-confirm")],
        key="users-disable-dialog"))


def _add_user(ctx: ViewContext, s: vm.AdminSession, redraw: Any) -> ft.Control:
    pal = ctx.pal
    roles = (s.data.get("users") or {}).get("roles") or []
    username = ft.TextField(label="Username", hint_text="jdoe", width=200, dense=True, key="users-new-username")
    name = ft.TextField(label="Display name", width=260, dense=True, key="users-new-name")
    role = ft.Dropdown(label="Role", width=190, dense=True, key="users-new-role", value="support",
                       options=[ft.DropdownOption(key=r["id"], text=r["label"]) for r in roles
                                if r["id"] != "owner" or s.user.get("role") == "owner"])

    def create(e: Any) -> None:
        vm.begin(s)
        s.temp_password = None
        try:
            result = s.client.create_user((username.value or "").strip().lower(), (name.value or "").strip(),
                                          role.value or "support")
            s.temp_password = {"username": result["user"]["username"], "password": result["temporary_password"]}
            s.notice = f"Created {result['user']['username']}."
        except AdminError as exc:
            s.errors["action"] = exc.message
        vm.load(s, ("users", "audit"))
        redraw("users", "audit")

    return card(
        text("Usernames: 3–32 lowercase letters, digits, dots, dashes. The server makes a temporary password.",
             pal, size=13, role="secondary"),
        ft.Row([username, name, role,
                ft.FilledButton("Add user", icon=ft.Icons.PERSON_ADD_ALT, on_click=create, key="users-create")],
               wrap=True, spacing=12, run_spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        pal=pal, title="Add a user", key="users-add")


def _roles_matrix(ctx: ViewContext, s: vm.AdminSession) -> ft.Control:
    roles = (s.data.get("users") or {}).get("roles") or []
    if not roles:
        return ft.Container()
    perms = list(PERMISSION_LABELS)
    dt = ft.DataTable(
        columns=[ft.DataColumn(ft.Text("Permission"))] + [ft.DataColumn(ft.Text(r["label"])) for r in roles],
        rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(PERMISSION_LABELS[p], size=13))] + [
            ft.DataCell(ft.Icon(ft.Icons.CHECK, size=18, color=ctx.pal.text_primary, tooltip="Allowed")
                        if p in r["permissions"] else ft.Text("—", size=13, color=ctx.pal.text_muted))
            for r in roles]) for p in perms],
        column_spacing=28, key="users-roles-matrix")
    return card(ft.Row([dt], scroll=ft.ScrollMode.AUTO),
                text("Owners can also manage other owners. Roles are fixed on the server so every change is "
                     "auditable.", ctx.pal, size=12, role="muted"),
                pal=ctx.pal, title="Roles & permissions", key="users-roles")
