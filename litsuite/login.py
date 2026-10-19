"""Sign-in helpers: email accounts, publisher websites and cloud storage."""

from __future__ import annotations

import getpass

from litsuite import config
from litsuite.fetch.publishers import LOGIN_PUBLISHERS, login_url, publishers, via_proxy
from litsuite.secrets import get_secret, set_secret, token_path

SSO_USER, SSO_PASS = "sso:username", "sso:password"


def login_email(settings, account: str, input_fn=input, password_fn=getpass.getpass, printer=print) -> str:
    cfg = settings.email_account(account)
    provider = (cfg.get("provider") or "").lower()
    if provider == "imap":
        from litsuite.mail.imap import ImapMailbox, secret_key

        printer(f"IMAP account '{account}' ({cfg.get('username')} @ {cfg.get('host')}).")
        printer("Tip: for Gmail/Yahoo/iCloud use an *app password*, not your normal password (see README).")
        pw = password_fn("Password / app password (input hidden): ")
        ImapMailbox(cfg, password=pw).close()  # fails loudly if wrong
        where = set_secret(secret_key(account), pw)
        return f"Connected. Password saved in {where}."
    if provider == "gmail":
        from litsuite.mail import gmail

        return gmail.login(cfg)
    if provider in ("outlook", "microsoft", "office365", "m365"):
        from litsuite.mail import outlook

        return outlook.login(cfg)
    if provider == "folder":
        return f"Nothing to sign in to: '{account}' reads saved emails from {cfg.get('path')}."
    raise ValueError(f"unknown provider '{provider}' for account '{account}'")


def store_sso_credentials(input_fn=input, password_fn=getpass.getpass) -> str:
    user = input_fn("University username / email: ").strip()
    pw = password_fn("University password (input hidden): ")
    set_secret(SSO_USER, user)
    where = set_secret(SSO_PASS, pw)
    return f"Saved in {where}. 'litsuite login publisher ... --autofill' will pre-fill sign-in forms."


def login_publishers(settings, names, autofill: bool = False, input_fn=input, printer=print, session=None) -> list:
    from litsuite.fetch.browser import BrowserSession

    access = settings.access
    table = publishers(access.get("publishers"))
    names = LOGIN_PUBLISHERS if not names or names == ["all"] else names
    unknown = [n for n in names if n not in table]
    if unknown:
        raise ValueError(f"unknown publisher(s): {', '.join(unknown)}. Known: {', '.join(table)}")
    user = get_secret(SSO_USER) if autofill else ""
    pw = get_secret(SSO_PASS) if autofill else ""
    done = []
    browser = session or BrowserSession.from_access(access, headless=False)
    with browser as b:
        if access.get("ezproxy_prefix") or access.get("openathens_domain"):
            # One sign-in at your library proxy covers every publisher.
            target = table[names[0]].get("home", "https://ieeexplore.ieee.org/")
            b.interactive_login(via_proxy(target, access), "Sign in at your library's proxy page.",
                                input_fn, printer, user, pw)
            return ["proxy"]
        for name in names:
            pub = table[name]
            url = login_url(pub, access)
            if not url:
                continue
            printer(f"\n=== {pub.get('name', name)} ===")
            b.interactive_login(url, pub.get("login_hint", ""), input_fn, printer, user, pw)
            done.append(name)
    return done


def check_publishers(settings, names=None, printer=print, session=None) -> dict:
    """Open each publisher's home page and look for 'Access provided by <institution>'."""
    from litsuite.fetch.browser import BrowserSession

    access = settings.access
    table = publishers(access.get("publishers"))
    names = names or [n for n in LOGIN_PUBLISHERS if table[n].get("access_markers")]
    inst = (access.get("institution") or "").lower()
    out = {}
    with (session or BrowserSession.from_access(access)) as b:
        for name in names:
            text = b.page_text(table[name].get("home", "")).lower()
            markers = [m.lower() for m in table[name].get("access_markers", [])]
            ok = (inst and inst in text) or any(m in text for m in markers)
            out[name] = bool(ok)
            printer(f"  {table[name].get('name', name):28s} {'signed in / institutional access' if ok else 'not detected'}")
    return out


def login_cloud(settings, which: str, input_fn=input, printer=print) -> str:
    cloud = settings.data.get("cloud") or {}
    if which == "dropbox":
        from litsuite.storage import dropbox_backend

        return dropbox_backend.login((cloud.get("dropbox") or {}).get("app_key", ""), input_fn, printer)
    if which == "gdrive":
        from litsuite.storage import gdrive

        return gdrive.login((cloud.get("gdrive") or {}).get("client_secret_file", ""))
    if which == "onedrive":
        from litsuite.storage import onedrive

        od = cloud.get("onedrive") or {}
        return onedrive.login(od.get("client_id", ""), od.get("tenant", "common"))
    if which == "rclone":
        return "rclone keeps its own logins: run 'rclone config' (https://rclone.org/remote_setup/)."
    raise ValueError("cloud must be one of: dropbox, gdrive, onedrive, rclone")


def status_lines(settings) -> list:
    lines = ["Email accounts:"]
    for name, cfg in (settings.data.get("email_accounts") or {}).items():
        provider = (cfg or {}).get("provider", "?")
        if provider == "imap":
            ok = bool(get_secret(f"imap:{name}"))
        elif provider == "gmail":
            from litsuite.mail.gmail import _auth

            ok = token_path("google", _auth({**(cfg or {}), "name": name})[1]).exists()
        elif provider in ("outlook", "microsoft", "office365", "m365"):
            from litsuite.mail.outlook import _auth as _ms_auth

            ok = token_path("msal", _ms_auth({**(cfg or {}), "name": name})[1]).exists()
        else:
            ok = True
        mark = "marks processed alerts read" if (cfg or {}).get("mark_as_read") else "read-only"
        lines.append(f"  {name:20s} {provider:8s} {'ready' if ok else 'NOT signed in -> litsuite login email ' + name}"
                     f"  ({mark}; only unread emails are opened)")
    if len(lines) == 1:
        lines.append("  (none configured)")
    lines.append("Cloud storage:")
    lines.append(f"  dropbox   {'connected' if get_secret('dropbox:refresh_token') else '-'}")
    lines.append(f"  gdrive    {'connected' if token_path('google', 'gdrive').exists() else '-'}")
    lines.append(f"  onedrive  {'connected' if token_path('msal', 'onedrive').exists() else '-'}")
    prof = config.APP_HOME / "browser" / (settings.access.get("browser") or {}).get("profile", "default")
    lines.append("Publisher browser session:")
    lines.append(f"  {prof} {'exists (run: litsuite login check)' if prof.exists() else 'not created yet -> litsuite login publisher all'}")
    lines.append(f"University SSO autofill: {'stored' if get_secret(SSO_USER) else 'not stored (optional)'}")
    return lines
