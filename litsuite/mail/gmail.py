"""Gmail through the official Gmail API (read-only access).

Settings::

    email_accounts:
      personal-gmail:
        provider: gmail
        address: me@gmail.com
        client_secret_file: "~/.litsuite/google_client_secret.json"   # see README > Gmail

Sign in once with ``litsuite login email personal-gmail`` (a browser tab opens).
Only unread messages are read (query ``is:unread``); fetching does not mark them
as read. ``mark_as_read: true`` asks for the gmail.modify permission instead of
read-only, so processed alerts can be marked as read.
Simpler alternative: Gmail over IMAP with an app password (provider: imap).
"""

from __future__ import annotations

import base64
import datetime as dt

from litsuite.mail.base import Mailbox, parse_mime, sender_matches
from litsuite.oauth import google_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MODIFY_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _auth(cfg: dict) -> tuple:
    if cfg.get("mark_as_read"):
        return MODIFY_SCOPES, f"gmail-{cfg['name']}-modify"
    return SCOPES, f"gmail-{cfg['name']}"


def login(cfg: dict) -> str:
    scopes, cache = _auth(cfg)
    google_credentials(cfg.get("client_secret_file", ""), scopes, cache, interactive=True)
    return f"Gmail account '{cfg['name']}' connected."


class GmailMailbox(Mailbox):
    def __init__(self, cfg: dict, service=None):
        self.name = cfg.get("name", "gmail")
        if service is None:
            from googleapiclient.discovery import build

            scopes, cache = _auth({**cfg, "name": self.name})
            creds = google_credentials(cfg.get("client_secret_file", ""), scopes, cache, interactive=False)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.svc = service
        self.extra_query = cfg.get("query", "")

    def search(self, senders, since: dt.date, limit: int = 300):
        froms = " OR ".join(s.lstrip("*") for s in senders)
        q = f"from:({froms}) is:unread after:{since.strftime('%Y/%m/%d')} {self.extra_query}".strip()
        users = self.svc.users()
        token, count = None, 0
        while True:
            resp = users.messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute()
            for ref in resp.get("messages", []):
                raw = users.messages().get(userId="me", id=ref["id"], format="raw").execute()["raw"]
                msg = parse_mime(base64.urlsafe_b64decode(raw.encode()), msg_id=ref["id"], account=self.name)
                if not sender_matches(msg.sender, senders):
                    continue
                msg.handle = ref["id"]
                yield msg
                count += 1
                if count >= limit:
                    return
            token = resp.get("nextPageToken")
            if not token:
                return

    def mark_read(self, messages) -> int:
        ids = [m.handle for m in messages if m.handle]
        for i in range(0, len(ids), 500):
            self.svc.users().messages().batchModify(
                userId="me", body={"ids": ids[i:i + 500], "removeLabelIds": ["UNREAD"]}).execute()
        return len(ids)
