"""A real browser (Playwright/Chromium) with a persistent profile.

You sign in to IEEE Xplore / ScienceDirect / Springer... once through your
university's single sign-on (MFA included) with ``litsuite login publisher``;
the cookies stay in ``~/.litsuite/browser/<profile>`` and later downloads reuse
them, even headless. Credentials are typed by you into the real login page;
litsuite can optionally pre-fill a username/password stored in your keyring.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

from litsuite import config

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"

USERNAME_SELECTORS = ["input[name=loginfmt]", "input#username", "input[name=username]", "input[name=j_username]",
                      "input[type=email]", "input[name=email]", "input[name=user]", "input[id*=user i]"]
PASSWORD_SELECTORS = ["input[name=passwd]", "input#password", "input[name=password]", "input[name=j_password]",
                      "input[type=password]"]
SUBMIT_SELECTORS = ["input#idSIButton9", "button[type=submit]", "input[type=submit]", "button[name=_eventId_proceed]"]


def is_pdf(data: bytes) -> bool:
    return bool(data) and PDF_MAGIC in data[:1024]


def find_pdf_links(html: str, base_url: str) -> list:
    """PDF links in an HTML page: citation_pdf_url meta, iframes/embeds, obvious PDF anchors."""
    out = []
    for pat in (r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
                r'<(?:iframe|embed)[^>]+src=["\']([^"\']+)',
                r'<a[^>]+href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
                r'<a[^>]+href=["\']([^"\']*(?:/pdf/|/pdfft|/pdfdirect/|stampPDF)[^"\']*)["\']'):
        for m in re.finditer(pat, html or "", re.IGNORECASE):
            link = urljoin(base_url, m.group(1).replace("&amp;", "&"))
            if link not in out and not link.startswith(("javascript:", "data:")):
                out.append(link)
    return out


class BrowserSession:
    def __init__(self, profile: str = "default", headless: bool = True, channel: str = "",
                 executable_path: str = "", timeout_seconds: int = 60, profile_dir=None):
        self.profile_dir = Path(profile_dir) if profile_dir else config.APP_HOME / "browser" / profile
        self.headless = headless
        self.channel = channel or None
        self.executable_path = executable_path or None
        self.timeout = int(timeout_seconds) * 1000
        self._pw = None
        self.context = None

    @classmethod
    def from_access(cls, access: dict, headless=None, profile_dir=None) -> "BrowserSession":
        b = (access or {}).get("browser") or {}
        return cls(b.get("profile", "default"), b.get("headless", True) if headless is None else headless,
                   b.get("channel", ""), b.get("executable_path", ""), b.get("timeout_seconds", 60), profile_dir)

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("Browser downloads need Playwright:\n  pip install playwright\n"
                               "  python -m playwright install chromium") from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs = dict(headless=self.headless, accept_downloads=True,
                      args=["--disable-blink-features=AutomationControlled"])
        if self.channel:
            kwargs["channel"] = self.channel
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        try:
            self.context = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise RuntimeError(f"Could not start the browser ({str(e).splitlines()[0]}). Install it with:\n"
                               "  python -m playwright install chromium\n"
                               "or set access.browser.channel to 'chrome' / 'msedge' in settings.yaml.") from e
        self.context.set_default_timeout(self.timeout)
        return self

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -- downloading -------------------------------------------------------
    def _request_pdf(self, url: str):
        try:
            resp = self.context.request.get(url, timeout=self.timeout, max_redirects=15)
        except Exception as e:
            log.debug("request %s failed: %s", url, e)
            return None, ""
        body = resp.body() if resp.ok else b""
        if is_pdf(body):
            return body, ""
        ctype = (resp.headers or {}).get("content-type", "")
        return None, body.decode("utf-8", errors="replace") if "html" in ctype or body[:200].strip().startswith(b"<") else ""

    def fetch_pdf(self, url: str, dest: Path, depth: int = 0) -> bool:
        """Download ``url`` (or the PDF it leads to) into ``dest``; True on success."""
        body, html = self._request_pdf(url)
        if body:
            dest.write_bytes(body)
            return True
        if html and depth < 2:
            for link in find_pdf_links(html, url)[:4]:
                if link != url and self.fetch_pdf(link, dest, depth + 1):
                    return True
        if depth > 0:
            return False
        return self._navigate_pdf(url, dest)

    def _navigate_pdf(self, url: str, dest: Path) -> bool:
        """Open the URL in a tab: handles JS redirects (ScienceDirect) and download prompts."""
        page = self.context.new_page()
        captured, downloads = {}, []

        def on_response(resp):
            try:
                if "body" not in captured and "application/pdf" in (resp.headers or {}).get("content-type", ""):
                    data = resp.body()
                    if is_pdf(data):
                        captured["body"] = data
            except Exception:
                pass

        def wait_for_pdf(seconds: float) -> bool:
            for _ in range(int(seconds * 4)):
                if downloads or captured:
                    return True
                page.wait_for_timeout(250)
            return bool(downloads or captured)

        page.on("response", on_response)
        page.on("download", lambda d: downloads.append(d))
        try:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:
                pass  # "Download is starting" aborts the navigation; that is fine
            if not wait_for_pdf(min(self.timeout / 1000, 15)):
                # Landing/viewer page: follow the PDF links it contains (IEEE stamp page, ScienceDirect...)
                try:
                    links = find_pdf_links(page.content(), page.url)[:3]
                except Exception:
                    links = []
                for link in links:
                    try:
                        page.goto(link, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    if wait_for_pdf(10):
                        break
            if downloads:
                downloads[0].save_as(str(dest))
                return is_pdf(dest.read_bytes()[:1024])
            if captured:
                dest.write_bytes(captured["body"])
                return True
            return False
        finally:
            page.close()

    # -- signing in ----------------------------------------------------------
    def interactive_login(self, url: str, hint: str = "", input_fn=input, printer=print,
                          username: str = "", password: str = "") -> None:
        page = self.context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        if username or password:
            autofill(page, username, password)
        printer(f"\nA browser window is open at:\n  {url}")
        if hint:
            printer(f"  {hint}")
        printer("Sign in with your university account (approve MFA if asked).")
        input_fn("When the site shows you are signed in, press Enter here to save the session... ")
        page.close()

    def page_text(self, url: str) -> str:
        page = self.context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            return page.inner_text("body")
        except Exception:
            return ""
        finally:
            page.close()


def autofill(page, username: str, password: str) -> bool:
    """Best-effort fill of common SSO forms (Microsoft Entra, Shibboleth, CAS, OpenAthens)."""
    filled = False
    for step in range(3):  # Microsoft asks for the username first, then the password
        for sel in USERNAME_SELECTORS:
            el = page.query_selector(sel)
            if username and el and el.is_visible() and not el.input_value():
                el.fill(username)
                filled = True
                break
        pw_el = None
        for sel in PASSWORD_SELECTORS:
            el = page.query_selector(sel)
            if el and el.is_visible():
                pw_el = el
                break
        if password and pw_el:
            pw_el.fill(password)
            filled = True
        for sel in SUBMIT_SELECTORS:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if pw_el:
            break
    return filled
