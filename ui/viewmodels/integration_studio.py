# ui/viewmodels/integration_studio.py
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Mapping
from zoneinfo import ZoneInfo

from core.integration.engine import defaults, preview_urls, test_response
from core.integration.schema import IntegrationSchema, IntegrationSchemaError
from core.integration.templates import TEMPLATES, UBER_DRIVER
from core.integration.uber_adapter import GigPlatformRegistry, WorkflowMapper


BLANK_SCHEMA: dict[str, Any] = {
    "format": 1,
    "id": "new-api-integration",
    "version": 1,
    "name": "New API Integration",
    "platform": "Platform",
    "description": "",
    "base_url": "https://api.example.com",
    "auth": {"type": "none"},
    "fields": [],
    "requests": [
        {
            "name": "records",
            "role": "records",
            "path": "/v1/records",
            "items_path": "",
            "mapping": {
                "occurred_at": ["timestamp"],
                "time_format": "iso8601",
                "amounts": {"gross": ["amount"]},
            },
        }
    ],
    "notes": "",
}


def _initial_state() -> dict[str, Any]:
    initial = json.dumps(UBER_DRIVER, indent=2)
    return {
        "open_id": None,
        "text": initial,
        "responses": {},
        "values": {},
        "urls": [],
        "result": None,
        "error": None,
        "notice": None,
        "history": [],
    }


def state(s: Any) -> dict[str, Any]:
    """Retrieve or initialize the integration studio state stored on the admin session."""
    studio = getattr(s, "studio", None)
    if studio is None:
        if not hasattr(s, "studio"):
            s.studio = {}
        studio = s.studio
    if "integration_state" not in studio:
        studio["integration_state"] = _initial_state()
    return studio["integration_state"]


def parse(st: dict[str, Any]) -> IntegrationSchema | None:
    text = st.get("text", "")
    try:
        data = json.loads(text)
        return IntegrationSchema.from_dict(data)
    except Exception as exc:
        if st.get("error") is not None:
            st["error"] = str(exc)
        return None


def form_values(st: dict[str, Any], schema: IntegrationSchema) -> dict[str, str]:
    stored = st.get("values") or {}
    def_vals = defaults(schema, date.today())
    return {**def_vals, **stored}


def summary(schema: IntegrationSchema) -> str:
    n_reqs = len(schema.requests)
    n_fields = len(schema.fields)
    return f"{schema.name} · {schema.platform} · {schema.base_url} ({n_reqs} request(s), {n_fields} field(s))"


def new(s: Any, template_name: str | None) -> None:
    st = state(s)
    st["open_id"] = None
    st["error"] = None
    st["notice"] = None
    st["responses"] = {}
    st["values"] = {}
    st["urls"] = []
    st["result"] = None
    st["history"] = []
    tpl = TEMPLATES.get(template_name) if template_name else BLANK_SCHEMA
    st["text"] = json.dumps(tpl or BLANK_SCHEMA, indent=2)


def open_schema(s: Any, schema_id: str) -> None:
    st = state(s)
    data = s.data.get("integrations") or {}
    items = data.get("integrations") or []
    entry = next((item for item in items if item.get("id") == schema_id), None)
    if not entry:
        raise ValueError(f"Integration {schema_id} not found")
    record = entry.get("draft") or entry.get("published")
    if not record:
        raise ValueError(f"Integration {schema_id} has no draft or published schema")
    st["open_id"] = schema_id
    st["error"] = None
    st["notice"] = None
    st["responses"] = {}
    st["values"] = {}
    st["urls"] = []
    st["result"] = None
    st["history"] = entry.get("history") or []
    st["text"] = json.dumps(record, indent=2)


def validate(st: dict[str, Any]) -> None:
    st["error"] = None
    st["notice"] = None
    try:
        data = json.loads(st.get("text", ""))
        schema = IntegrationSchema.from_dict(data)
        st["notice"] = f"Valid: {summary(schema)}"
    except Exception as exc:
        st["error"] = str(exc)


def reformat(st: dict[str, Any]) -> None:
    st["error"] = None
    try:
        data = json.loads(st.get("text", ""))
        st["text"] = json.dumps(data, indent=2)
    except Exception as exc:
        st["error"] = f"Invalid JSON: {exc}"


def preview(st: dict[str, Any]) -> None:
    st["error"] = None
    try:
        data = json.loads(st.get("text", ""))
        schema = IntegrationSchema.from_dict(data)
        tz = ZoneInfo("UTC")
        values = form_values(st, schema)
        st["urls"] = preview_urls(schema, values, tz=tz)
    except Exception as exc:
        st["error"] = str(exc)


def run_test(st: dict[str, Any]) -> None:
    st["error"] = None
    try:
        data = json.loads(st.get("text", ""))
        schema = IntegrationSchema.from_dict(data)
        responses = st.get("responses") or {}
        values = form_values(st, schema)
        st["result"] = test_response(schema, responses, values)
    except Exception as exc:
        st["error"] = str(exc)


def save(s: Any) -> None:
    st = state(s)
    st["error"] = None
    st["notice"] = None
    try:
        data = json.loads(st.get("text", ""))
        schema = IntegrationSchema.from_dict(data)
    except Exception as exc:
        st["error"] = f"Cannot save invalid schema: {exc}"
        return

    # Call admin session helper or update server data store directly
    if hasattr(s, "save_integration_draft"):
        s.save_integration_draft(schema.id, schema.to_dict())
    else:
        integrations_data = s.data.setdefault("integrations", {})
        lst = integrations_data.setdefault("integrations", [])
        entry = next((item for item in lst if item.get("id") == schema.id), None)
        if entry is None:
            entry = {"id": schema.id, "draft": schema.to_dict(), "published": None, "history": []}
            lst.append(entry)
        else:
            entry["draft"] = schema.to_dict()
    st["open_id"] = schema.id
    st["notice"] = f"Saved draft for {schema.id} v{schema.version}"


def publish(s: Any) -> None:
    st = state(s)
    st["error"] = None
    st["notice"] = None
    schema_id = st.get("open_id")
    if not schema_id:
        st["error"] = "Save draft before publishing"
        return
    if hasattr(s, "publish_integration"):
        s.publish_integration(schema_id)
    else:
        integrations_data = s.data.setdefault("integrations", {})
        lst = integrations_data.setdefault("integrations", [])
        entry = next((item for item in lst if item.get("id") == schema_id), None)
        if entry and entry.get("draft"):
            entry["published"] = dict(entry["draft"])
    st["notice"] = f"Published {schema_id}"


class IntegrationStudioViewModel:
    def __init__(self):
        self.selected_platform: str = "Uber Driver API"
        self.source_fields: List[Dict[str, str]] = GigPlatformRegistry.fetch_source_fields(self.selected_platform)
        self.active_mappings: List[Dict[str, str]] = []

    def change_platform(self, platform: str):
        self.selected_platform = platform
        self.source_fields = GigPlatformRegistry.fetch_source_fields(platform)
        self.active_mappings.clear()

    def add_mapping(self, source_field: str, target_column: str):
        self.active_mappings.append({"source_field": source_field, "target_column": target_column})

    def generate_code(self) -> Dict[str, Any]:
        return WorkflowMapper.build_query_payload(self.active_mappings)
