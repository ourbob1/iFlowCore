"""
Starting points for the Admin Console's Integration Studio ("Start from a template").

UBER_DRIVER is written against Uber's documented Driver API (api.uber.com, /v1/partners/*,
OAuth scopes partner.trips / partner.payments). Two things to know before publishing it:

  • Access: Uber grants the partner.* scopes to approved apps only. Until Uber approves iFlow and
    issues a client id, the connection can't complete. Put the real client id in auth.client_id and
    register the redirect http://127.0.0.1:18766/callback with Uber.
  • Field names and payment categories below follow Uber's public reference and haven't been
    checked against a live response. Paste a real (redacted) response into the Studio's test bench
    and adjust the mapping before publishing.

Money comes from /payments (one record per payment, so tips and promotions dedupe on their own);
/trips is a "details" request joined on trip_id for the pickup time, distance and duration.
"""

from __future__ import annotations

from typing import Any

UBER_DRIVER: dict[str, Any] = {
    "format": 1,
    "id": "uber-driver-api",
    "version": 1,
    "name": "Uber driver earnings",
    "platform": "Uber",
    "description": "Connect your Uber driver account to bring in trips, tips and fees automatically.",
    "base_url": "https://api.uber.com",
    "auth": {
        "type": "oauth2_pkce",
        "authorize_url": "https://auth.uber.com/oauth/v2/authorize",
        "token_url": "https://auth.uber.com/oauth/v2/token",
        "client_id": "REPLACE-WITH-UBER-CLIENT-ID",
        "scopes": ["partner.accounts", "partner.trips", "partner.payments"],
        "token_exchange": "broker",           # Uber issues confidential clients; the secret stays on iFlow's API
        "redirect_port": 18766,
    },
    "fields": [
        {"key": "start_date", "label": "From", "type": "date", "required": True, "default": "today-30d",
         "help": "Earnings on or after this day"},
        {"key": "end_date", "label": "To", "type": "date", "required": True, "default": "today"},
    ],
    "requests": [
        {
            "name": "trips", "role": "details", "path": "/v1/partners/trips", "items_path": "trips",
            "key_path": "trip_id",
            "query": {"from_time": "{start_date|epoch}", "to_time": "{end_date|epoch_end}"},
            "pagination": {"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 50,
                           "max_pages": 20, "total_path": "count"},
        },
        {
            "name": "payments", "role": "records", "path": "/v1/partners/payments", "items_path": "payments",
            "query": {"from_time": "{start_date|epoch}", "to_time": "{end_date|epoch_end}"},
            "pagination": {"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 50,
                           "max_pages": 20, "total_path": "count"},
            "mapping": {
                "occurred_at": ["detail.pickup.timestamp", "event_time"],
                "time_format": "epoch_s",
                "reference": ["payment_id"],
                "currency": ["currency_code"],
                "default_currency": "USD",
                "kind_path": "category",
                "kind_values": {"fare": "earning", "tip": "tip", "promotion": "bonus", "incentive": "bonus",
                                "toll": "reimbursement", "other": "adjustment", "cash_collected": "skip",
                                "device_payment": "skip", "vehicle_payment": "skip"},
                # amount is what Uber paid out: add back the (negative) service fee, take tolls out to
                # their own pass-through record
                "amounts": {"gross": ["amount", "-breakdown.service_fee", "-breakdown.toll"],
                            "tolls": "breakdown.toll"},
                "fees": {"paths": ["breakdown.service_fee"], "multiplier_bp": -10000},
                "description": "Uber trip {trip_id} · {detail.distance} mi · {detail.duration} s",
            },
        },
    ],
    "notes": "Unverified against a live Uber response: check categories and breakdown fields in the test bench "
             "before publishing. Needs Uber's approval for partner.* scopes and a real client id.",
}

TEMPLATES: dict[str, dict[str, Any]] = {"Uber (driver API)": UBER_DRIVER}
