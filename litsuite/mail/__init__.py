"""Email access for alert processing. Providers: imap, gmail, outlook, folder."""

from __future__ import annotations

from litsuite.mail.base import MailMessage, Mailbox, sender_matches  # noqa: F401

__all__ = ["MailMessage", "Mailbox", "sender_matches", "open_mailbox", "PROVIDERS"]

PROVIDERS = ("imap", "gmail", "outlook", "folder")


def open_mailbox(cfg: dict) -> Mailbox:
    provider = (cfg.get("provider") or "").lower()
    if provider == "imap":
        from litsuite.mail.imap import ImapMailbox

        return ImapMailbox(cfg)
    if provider == "gmail":
        from litsuite.mail.gmail import GmailMailbox

        return GmailMailbox(cfg)
    if provider in ("outlook", "microsoft", "office365", "m365"):
        from litsuite.mail.outlook import OutlookMailbox

        return OutlookMailbox(cfg)
    if provider == "folder":
        from litsuite.mail.folder import FolderMailbox

        return FolderMailbox(cfg)
    raise ValueError(f"email account '{cfg.get('name')}': unknown provider '{provider}' "
                     f"(use one of: {', '.join(PROVIDERS)})")
