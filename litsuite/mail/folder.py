"""Alert emails saved as files: .eml (any mail app: drag & drop / "Save as"),
.mbox (Thunderbird, Google Takeout), or .html. No credentials needed.

Messages flagged as read inside the file (mbox ``Status: RO``, Thunderbird
``X-Mozilla-Status``) are skipped; files without such flags count as unread.

Settings::

    email_accounts:
      saved-alerts:
        provider: folder
        path: "~/Documents/ScholarAlerts"
"""

from __future__ import annotations

import datetime as dt
import mailbox
from pathlib import Path

from litsuite.mail.base import Mailbox, MailMessage, parse_mime, sender_matches


class FolderMailbox(Mailbox):
    def __init__(self, cfg: dict):
        self.name = cfg.get("name", "folder")
        self.path = Path(str(cfg.get("path", ""))).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"email account '{self.name}': folder not found: {self.path}")
        self.assume_sender = cfg.get("assume_sender", "")

    def _messages(self):
        files = [self.path] if self.path.is_file() else sorted(self.path.rglob("*"))
        for f in files:
            suffix = f.suffix.lower()
            if suffix == ".eml":
                yield parse_mime(f.read_bytes(), msg_id=f"file:{f.name}", account=self.name)
            elif suffix == ".mbox":
                for i, m in enumerate(mailbox.mbox(str(f))):
                    yield parse_mime(m.as_bytes(), msg_id=f"mbox:{f.name}:{i}", account=self.name)
            elif suffix in (".html", ".htm"):
                html = f.read_text(encoding="utf-8", errors="replace")
                sender = self.assume_sender
                if not sender and "gse_alrt" in html:
                    sender = "scholaralerts-noreply@google.com"
                yield MailMessage(id=f"file:{f.name}", subject=f.stem, sender=sender,
                                  date=dt.datetime.fromtimestamp(f.stat().st_mtime), html=html, account=self.name)

    def search(self, senders, since: dt.date, limit: int = 300):
        count = 0
        for msg in self._messages():
            if msg.read or (msg.date and msg.date.date() < since):
                continue
            if not sender_matches(msg.sender, senders):
                continue
            yield msg
            count += 1
            if count >= limit:
                return
