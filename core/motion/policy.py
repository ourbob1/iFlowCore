"""
Location-permission policy for the motion sentry.

Store and OS rules this encodes:

* **Google Play (Android)** — background location needs a *prominent in-app
  disclosure* shown before the permission prompt, explaining what is
  collected and why; the Play Console must declare the background-location
  use case. Android 10+ grants "Allow all the time" separately from "while
  using the app" (on 11+ only through system settings), and Android 14+
  requires a location-type foreground service with a visible notification
  while tracking in the background.
* **Apple (iOS)** — "While Using" first, then an upgrade to "Always";
  `UIBackgroundModes: location` and usage descriptions in Info.plist; the
  blue background-location indicator is shown while tracking.
* **Desktop / web** — no reliable GPS or background execution: the sentry is
  off and trips are logged manually.

`next_step` turns (platform, current permission, user choice) into the one
thing the UI should do next, so the flow is testable without a device.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Platform = Literal["android", "ios", "macos", "windows", "linux", "web", "fuchsia"]
PermissionStatus = Literal["denied", "deniedForever", "whileInUse", "always", "unableToDetermine"]
Action = Literal[
    "unsupported", "show_disclosure", "request_permission", "open_app_settings",
    "run_foreground_only", "run_background",
]

MOBILE = frozenset({"android", "ios"})

DISCLOSURE_TITLE = "Automatic trip detection"
DISCLOSURE_TEXT = (
    "iFlow Core collects your location to detect when you start and stop driving, "
    "including when the app is closed or not in use, so your business miles are "
    "logged automatically for your mileage deduction. Location stays encrypted on "
    "this device; only trip distance and times are kept, never your route. "
    "You can turn this off anytime in Settings."
)
ANDROID_NOTIFICATION_TITLE = "Trip detection is on"
ANDROID_NOTIFICATION_TEXT = "iFlow Core is watching for drives to log your miles."


@dataclass(frozen=True)
class PermissionStep:
    action: Action
    message: str


def is_mobile(platform: str) -> bool:
    return platform.lower() in MOBILE


def next_step(platform: str, status: str | None, *, want_background: bool, disclosure_accepted: bool) -> PermissionStep:
    platform = platform.lower()
    if platform not in MOBILE:
        return PermissionStep("unsupported", "Automatic trip detection needs a phone. Log trips manually on desktop.")
    if not disclosure_accepted:
        return PermissionStep("show_disclosure", DISCLOSURE_TEXT)
    if status in (None, "denied", "unableToDetermine"):
        return PermissionStep("request_permission", "Allow location access to detect trips.")
    if status == "deniedForever":
        return PermissionStep("open_app_settings", "Location is blocked. Enable it for iFlow Core in system settings.")
    if status == "whileInUse":
        if not want_background:
            return PermissionStep("run_foreground_only", "Trips are detected while iFlow Core is open.")
        if platform == "android":
            return PermissionStep(
                "open_app_settings",
                "To detect trips with the app closed, choose “Allow all the time” in Location permissions.",
            )
        return PermissionStep("request_permission", "Choose “Change to Always Allow” to detect trips in the background.")
    if status == "always":
        return PermissionStep("run_background" if want_background else "run_foreground_only", "Trip detection is on.")
    return PermissionStep("request_permission", "Allow location access to detect trips.")
