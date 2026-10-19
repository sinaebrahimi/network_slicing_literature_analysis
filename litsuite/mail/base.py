"""Mailbox interface and MIME helpers shared by all email providers."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import fnmatch
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.utils import parseaddr, parsedate_to_datetime


@dataclass
class MailMessage:
    """One email. ``handle`` is the provider's own ID (used to mark it as read)."""

    id: str
    subject: str = ""
    sender: str = ""          # bare address, lower case
    date: dt.datetime | None = None
    html: str = ""
    text: str = ""
    account: str = ""
    read: bool = False
    handle: object = None
    extra: dict = field(default_factory=dict)


def sender_matches(sender: str, patterns) -> bool:
    """Patterns: exact address, '@domain' (domain or sub-domain), or a glob ('*@acm.org')."""
    s = (sender or "").lower().strip()
    for pat in patterns or ():
        p = str(pat).lower().strip()
        if not p:
            continue
        if p.startswith("@"):
            domain = s.rsplit("@", 1)[-1]
            if domain == p[1:] or domain.endswith("." + p[1:]):
                return True
        elif any(ch in p for ch in "*?["):
            if fnmatch.fnmatch(s, p):
                return True
        elif s == p:
            return True
    return False


def parse_mime(raw: bytes, msg_id: str = "", account: str = "") -> MailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    html_parts, text_parts = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        (html_parts if ctype == "text/html" else text_parts).append(content)
    # Read flags written by mail clients into saved/exported messages (mbox, Thunderbird).
    status = str(msg.get("Status") or "") + str(msg.get("X-Status") or "")
    read = "R" in status
    moz = str(msg.get("X-Mozilla-Status") or "").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{1,8}", moz):
        read = read or bool(int(moz, 16) & 0x1)
    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(str(msg["Date"]))
        except (TypeError, ValueError):
            date = None
    return MailMessage(
        id=str(msg.get("Message-ID") or msg_id or "").strip(),
        subject=str(msg.get("Subject") or ""),
        sender=parseaddr(str(msg.get("From") or ""))[1].lower(),
        date=date,
        html="\n".join(html_parts),
        text="\n".join(text_parts),
        account=account,
        read=read,
    )


class Mailbox(ABC):
    """Every provider returns UNREAD messages only: litsuite never opens an email you
    have already read. Messages are fetched without changing their read state; they are
    marked as read afterwards only if the account sets ``mark_as_read: true``."""

    name: str = ""

    @abstractmethod
    def search(self, senders, since: dt.date, limit: int = 300):
        """Yield unread MailMessage objects from any of ``senders`` received on/after ``since``."""

    def mark_read(self, messages) -> int:
        """Mark messages as read in the mailbox; returns how many were marked."""
        return 0

    def close(self) -> None:
        pass
