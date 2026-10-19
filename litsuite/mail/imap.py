"""Any IMAP mailbox: Gmail (with an app password), Yahoo, iCloud, Fastmail,
university mail servers (Dovecot/Exchange with IMAP enabled)...

Settings::

    email_accounts:
      uni-imap:
        provider: imap
        host: imap.example.ac.uk     # Gmail: imap.gmail.com   iCloud: imap.mail.me.com
        port: 993
        username: me@example.ac.uk
        folders: [INBOX]             # Gmail: add "[Gmail]/All Mail" to include archived mail

Store the password once with ``litsuite login email uni-imap``. Only UNSEEN
(unread) messages are searched, and they are fetched with BODY.PEEK so reading
them does not change their state. With ``mark_as_read: true`` the processed
alerts are flagged as read at the end of a run.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import logging

from litsuite.mail.base import Mailbox, parse_mime, sender_matches
from litsuite.secrets import get_secret

log = logging.getLogger(__name__)


def secret_key(account_name: str) -> str:
    return f"imap:{account_name}"


class ImapMailbox(Mailbox):
    def __init__(self, cfg: dict, password: str | None = None, imap_factory=None):
        self.name = cfg.get("name", "imap")
        self.host = cfg.get("host") or "imap.gmail.com"
        self.port = int(cfg.get("port") or 993)
        self.username = cfg.get("username") or cfg.get("address")
        self.folders = cfg.get("folders") or [cfg.get("folder") or "INBOX"]
        self.password = password or get_secret(secret_key(self.name))
        if not self.username:
            raise ValueError(f"email account '{self.name}': set 'username'")
        if not self.password:
            raise PermissionError(f"No password stored for '{self.name}'. Run: litsuite login email {self.name}")
        factory = imap_factory or (lambda: imaplib.IMAP4_SSL(self.host, self.port))
        self.conn = factory()
        self.conn.login(self.username, self.password)

    @staticmethod
    def _quote(folder: str) -> str:
        return f'"{folder}"' if " " in folder and not folder.startswith('"') else folder

    def search(self, senders, since: dt.date, limit: int = 300):
        since_str = since.strftime("%d-%b-%Y")
        count = 0
        for folder in self.folders:
            typ, _ = self.conn.select(self._quote(folder), readonly=True)
            if typ != "OK":
                log.warning("IMAP folder not found: %s", folder)
                continue
            uids = set()
            for s in senders:
                term = s.lstrip("@*") if s.startswith(("@", "*")) else s
                typ, data = self.conn.uid("SEARCH", None, "UNSEEN", "SINCE", since_str, "FROM", f'"{term}"')
                if typ == "OK" and data and data[0]:
                    uids.update(data[0].split())
            for uid in sorted(uids, key=int, reverse=True):
                typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                msg = parse_mime(data[0][1], msg_id=f"{self.name}:{folder}:{uid.decode()}", account=self.name)
                if not sender_matches(msg.sender, senders):
                    continue
                msg.handle = (folder, uid.decode())
                yield msg
                count += 1
                if count >= limit:
                    return

    def mark_read(self, messages) -> int:
        by_folder: dict = {}
        for m in messages:
            if m.handle:
                by_folder.setdefault(m.handle[0], []).append(m.handle[1])
        done = 0
        for folder, uids in by_folder.items():
            if self.conn.select(self._quote(folder), readonly=False)[0] != "OK":
                continue
            if self.conn.uid("STORE", ",".join(uids), "+FLAGS", "(\\Seen)")[0] == "OK":
                done += len(uids)
        return done

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:
            pass
