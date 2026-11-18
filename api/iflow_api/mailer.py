"""Transactional email: verification and reset codes. Messages carry the code and nothing else."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("iflow_api.mail")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    def send(self, message: Message) -> None: ...


def verification_message(to: str, code: str, ttl_min: int) -> Message:
    return Message(to, f"{code} is your iFlow Core verification code",
                   f"Your iFlow Core verification code is {code}.\n\nIt expires in {ttl_min} minutes. "
                   "If you didn't create an account, you can ignore this email.\n")


def reset_message(to: str, code: str, ttl_min: int) -> Message:
    return Message(to, f"{code} is your iFlow Core password reset code",
                   f"Use {code} to reset your iFlow Core password. It expires in {ttl_min} minutes.\n\n"
                   "If you didn't ask for this, ignore this email; your password hasn't changed.\n")


def existing_account_message(to: str) -> Message:
    return Message(to, "Someone tried to sign up with your email",
                   "Someone tried to create an iFlow Core account with this email address, which already "
                   "has an account. If it was you, sign in instead (or reset your password). "
                   "If it wasn't, you can ignore this email.\n")


@dataclass
class MemoryMailer:
    """Tests: keeps messages in a list."""

    sent: list[Message] = field(default_factory=list)

    def send(self, message: Message) -> None:
        self.sent.append(message)


class ConsoleMailer:
    """Development only (refused in production): prints the message so codes can be typed in."""

    def send(self, message: Message) -> None:
        log.warning("DEV MAIL to %s: %s\n%s", message.to, message.subject, message.body)


class SESMailer:
    def __init__(self, sender: str, region: str | None) -> None:
        import boto3                                    # only needed in the AWS deployment

        self._client = boto3.client("sesv2", region_name=region)
        self._sender = sender

    def send(self, message: Message) -> None:
        self._client.send_email(
            FromEmailAddress=self._sender, Destination={"ToAddresses": [message.to]},
            Content={"Simple": {"Subject": {"Data": message.subject, "Charset": "UTF-8"},
                                "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}}}})


def make_mailer(backend: str, sender: str, region: str | None) -> Mailer:
    if backend == "ses":
        return SESMailer(sender, region)
    if backend == "memory":
        return MemoryMailer()
    return ConsoleMailer()
