"""Outlook.com / Microsoft 365 (university or work) mail through Microsoft Graph.

Microsoft no longer accepts plain passwords over IMAP, so this uses a one-time
browser sign-in (device code, MFA supported) with read-only ``Mail.Read``.

Settings::

    email_accounts:
      uni-outlook:
        provider: outlook
        address: me@university.ac.uk
        client_id: "00000000-0000-0000-0000-000000000000"   # Azure app registration, see README > Outlook
        tenant: organizations      # "consumers" for @outlook.com/@hotmail.com, or your tenant ID

Sign in once with ``litsuite login email uni-outlook``. Only unread messages
(``isRead eq false``) are read; ``mark_as_read: true`` requests ``Mail.ReadWrite``
so processed alerts can be marked as read.
"""

from __future__ import annotations

import datetime as dt

import requests

from litsuite.mail.base import Mailbox, MailMessage, sender_matches
from litsuite.oauth import GRAPH, microsoft_token

SCOPES = ["Mail.Read"]
WRITE_SCOPES = ["Mail.ReadWrite"]


def _auth(cfg: dict) -> tuple:
    if cfg.get("mark_as_read"):
        return WRITE_SCOPES, f"mail-{cfg['name']}-rw"
    return SCOPES, f"mail-{cfg['name']}"


def login(cfg: dict) -> str:
    scopes, cache = _auth(cfg)
    microsoft_token(cfg.get("client_id", ""), scopes, cache, cfg.get("tenant", "common"))
    return f"Outlook account '{cfg['name']}' connected."


class OutlookMailbox(Mailbox):
    def __init__(self, cfg: dict, session=None, token: str | None = None):
        self.name = cfg.get("name", "outlook")
        self.http = session or requests.Session()
        if token is None:
            scopes, cache = _auth({**cfg, "name": self.name})
            token = microsoft_token(cfg.get("client_id", ""), scopes, cache, cfg.get("tenant", "common"),
                                    interactive=False)
        self.http.headers.update({"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="html"'})

    def search(self, senders, since: dt.date, limit: int = 300):
        start = dt.datetime.combine(since, dt.time()).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"{GRAPH}/me/messages?$filter=receivedDateTime ge {start} and isRead eq false&$top=100"
               "&$select=id,subject,from,receivedDateTime,internetMessageId,isRead,body&$orderby=receivedDateTime desc")
        count = 0
        while url:
            r = self.http.get(url, timeout=60)
            r.raise_for_status()
            data = r.json()
            for m in data.get("value", []):
                sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                if not sender_matches(sender, senders):
                    continue
                body = m.get("body") or {}
                received = m.get("receivedDateTime")
                yield MailMessage(
                    id=m.get("internetMessageId") or m["id"], subject=m.get("subject") or "", sender=sender,
                    date=dt.datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None,
                    html=body.get("content", "") if body.get("contentType") == "html" else "",
                    text=body.get("content", "") if body.get("contentType") != "html" else "",
                    account=self.name, read=bool(m.get("isRead")), handle=m["id"])
                count += 1
                if count >= limit:
                    return
            url = data.get("@odata.nextLink")

    def mark_read(self, messages) -> int:
        done = 0
        for m in messages:
            if m.handle:
                r = self.http.patch(f"{GRAPH}/me/messages/{m.handle}", json={"isRead": True}, timeout=60)
                r.raise_for_status()
                done += 1
        return done
