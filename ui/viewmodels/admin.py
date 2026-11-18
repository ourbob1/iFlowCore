"""
Admin Console view-model. Holds the signed-in session in memory and shapes server data for
display. It never reads or writes any user's data: everything comes from the AI service's admin
API (accounts, licenses, statement formats, scrubbed crash reports, server health).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.admin.client import AdminClient, AdminError
from ui.format import money

SESSION_KEY = "admin_session"
SECTIONS = ("overview", "plans", "licenses", "revenue", "issues", "health", "audit", "users", "parsers",
            "integrations")
# what each section needs (None: any signed-in user)
SECTION_PERMISSION = {"overview": None, "plans": "licenses.read", "licenses": "licenses.read",
                      "revenue": "licenses.read", "issues": "telemetry.read", "health": "health.read",
                      "audit": "audit.read", "users": "users.manage", "parsers": "parsers.edit",
                      "integrations": "parsers.edit"}
TIER_NAMES = {"pro": "iFlow Pro", "guild": "iFlow Health & Guild"}


@dataclass
class AdminSession:
    client: AdminClient
    actor: str
    license_signing: bool
    user: dict[str, Any] = field(default_factory=dict)
    permissions: frozenset[str] = frozenset()
    must_change_password: bool = False
    temp_password: dict[str, str] | None = None     # {"username", "password"}: shown once after create/reset
    studio: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    issued: dict[str, Any] | None = None           # the license just issued (shown once, then dropped)
    verified: dict[str, Any] | None = None
    issue_filter: str = "open"
    expanded: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    notice: str | None = None
    expired: bool = False                             # the server ended the session (idle / absolute timeout)

    def can(self, permission: str | None) -> bool:
        return permission is None or permission in self.permissions


def session(services: dict[str, Any]) -> AdminSession | None:
    return services.get(SESSION_KEY)


def _session_from(client: AdminClient, me: dict[str, Any]) -> AdminSession:
    return AdminSession(client, str(me.get("actor", "admin")), bool(me.get("license_signing")),
                        user=me.get("user") or {}, permissions=frozenset(me.get("permissions") or ()),
                        must_change_password=bool(me.get("must_change_password")))


def sign_in(services: dict[str, Any], url: str, token: str, *, transport: Any = None) -> AdminSession:
    """Service-token sign-in (automation / break-glass)."""
    client = AdminClient(url, token, transport=transport)
    s = _session_from(client, client.me())          # raises AdminError on a bad token / unreachable server
    services[SESSION_KEY] = s
    load(s)
    return s


def sign_in_password(services: dict[str, Any], url: str, username: str, password: str, *,
                     transport: Any = None) -> AdminSession:
    client, _ = AdminClient.login(url, username, password, transport=transport)
    s = _session_from(client, client.me())
    services[SESSION_KEY] = s
    if not s.must_change_password:
        load(s)
    return s


def change_password(s: AdminSession, current: str, new: str, confirm: str) -> None:
    if new != confirm:
        raise AdminError("The new passwords don't match")
    s.client.change_password(current, new)
    me = s.client.me()
    s.must_change_password = bool(me.get("must_change_password"))
    s.user = me.get("user") or s.user
    load(s)


def sign_out(services: dict[str, Any]) -> None:
    s = services.pop(SESSION_KEY, None)              # drops the only copy of the token
    if s is not None:
        s.client.logout()


def begin(s: AdminSession) -> None:
    """Start of a user action: drop the previous action's messages."""
    s.notice = None
    for name in ("issue", "action"):
        s.errors.pop(name, None)


def load(s: AdminSession, sections: tuple[str, ...] = SECTIONS) -> None:
    calls = {"overview": s.client.overview, "plans": s.client.plans, "licenses": lambda: s.client.licenses(15),
             "revenue": lambda: s.client.revenue(12), "issues": lambda: s.client.issues(s.issue_filter, 50),
             "health": s.client.health, "audit": lambda: s.client.audit(25), "users": s.client.users,
             "parsers": s.client.parsers, "integrations": s.client.integrations}
    for name in sections:
        if not s.can(SECTION_PERMISSION.get(name)):
            continue
        try:
            s.data[name] = calls[name]()
            s.errors.pop(name, None)
        except AdminError as exc:
            s.errors[name] = exc.message
            if exc.status == 401:
                s.expired = True
                return


# --- formatting ---------------------------------------------------------------------------------------


def cents(value: Any, currency: str = "USD") -> str:
    """Money from the server is integer cents; anything else is shown as unknown, never guessed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "—"
    return money(value, currency)


def duration(seconds: Any) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def size(n: Any) -> str:
    if not isinstance(n, int) or n < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def short_date(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%b %d, %Y")
    except ValueError:
        return "—"


def plan_options(s: AdminSession) -> list[tuple[str, str]]:
    """(value, label) pairs for the plans the server can issue, e.g. ('pro:annual', 'iFlow Pro · annual · $60.00')."""
    out = []
    for p in (s.data.get("plans") or {}).get("plans", []):
        out.append((f"{p['tier']}:{p['period']}",
                    f"{TIER_NAMES.get(p['tier'], p['tier'])} · {p['period']} · {cents(p['amount_minor'], p['currency'])}"))
    return out


def health_rows(h: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(label, value, level) for the health card; level is good / warning / serious."""
    if not h:
        return []
    ocr = h.get("ocr") or {}
    q = h.get("queue") or {}
    req = h.get("requests") or {}
    lat = req.get("latency_ms") or {}
    disk = h.get("disk") or {}
    models = h.get("models") or {}
    broken = [k for k, v in models.items() if str(v).startswith("error")]
    no_models = bool(models) and all(str(v) == "off" for v in models.values())    # e.g. the console's built-in service
    classes = req.get("by_class") or {}
    errors_5xx = int(classes.get("5xx", 0))
    free = disk.get("free_bytes")
    total = disk.get("total_bytes")
    low_disk = isinstance(free, int) and isinstance(total, int) and total > 0 and free * 10 < total
    return [
        ("Service", f"v{h.get('version', '?')} · Python {h.get('python', '?')}"
                    + (" · in a container" if h.get("container") else ""), "good"),
        ("Uptime", duration(h.get("uptime_s")), "good"),
        ("OCR (Tesseract)", f"ready · {ocr.get('version')}" if ocr.get("available") else
         "not used here (no AI models on this service)" if no_models else
         f"unavailable ({ocr.get('error', 'unknown')})", "good" if ocr.get("available") or no_models else "serious"),
        ("Models", "none (admin only)" if no_models else ", ".join(f"{k}: {v}" for k, v in sorted(models.items())) or "none",
         "serious" if broken else "good"),
        ("Queue", f"{q.get('slots_free', '?')} of {q.get('max_concurrency', '?')} slots free · "
                  f"{q.get('waiting', 0)} waiting", "warning" if q.get("waiting") else "good"),
        ("Requests", f"{req.get('total', 0)} since start · " + " · ".join(f"{k} {v}" for k, v in sorted(classes.items())),
         "warning" if errors_5xx else "good"),
        ("Latency", f"p50 {lat.get('p50', 0)} ms · p95 {lat.get('p95', 0)} ms", "good"),
        ("Rate-limited", f"{req.get('rate_limited', 0)} requests (429)",
         "warning" if req.get("rate_limited") else "good"),
        ("Disk", f"{size(free)} free of {size(total)} · databases {size(disk.get('databases_bytes'))}",
         "serious" if low_disk else "good"),
    ]
