"""One polite HTTP session for all web/API calls (rate limits, retries, contact email)."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import requests

from litsuite import __version__

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Minimum seconds between two requests to the same host.
HOST_DELAYS = {
    "api.crossref.org": 0.3,
    "api.semanticscholar.org": 1.0,
    "api.openalex.org": 0.2,
    "api.unpaywall.org": 0.2,
    "export.arxiv.org": 3.0,
    "doi.org": 0.7,
}


class Http:
    def __init__(self, email: str = "", delays: dict | None = None):
        self.email = email
        self.delays = {**HOST_DELAYS, **(delays or {})}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._last: dict = {}
        self._lock = threading.Lock()

    def _api_headers(self, host: str) -> dict:
        if host.startswith("api.") or host == "export.arxiv.org":
            ua = f"litsuite/{__version__} (https://github.com/sinaebrahimi/network_slicing_literature_analysis"
            ua += f"; mailto:{self.email})" if self.email else ")"
            return {"User-Agent": ua}
        return {}

    def _throttle(self, host: str) -> None:
        delay = self.delays.get(host, 0)
        if not delay:
            return
        with self._lock:
            wait = self._last.get(host, 0) + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def get(self, url: str, params=None, timeout: float = 20, retries: int = 2, **kw):
        """GET with throttling and retry on 429/5xx. Returns Response or None on network error."""
        host = urlparse(url).netloc.lower()
        headers = {**self._api_headers(host), **kw.pop("headers", {})}
        if self.email and host == "api.crossref.org":
            params = {**(params or {}), "mailto": self.email}
        for attempt in range(retries + 1):
            self._throttle(host)
            try:
                r = self.session.get(url, params=params, timeout=timeout, headers=headers, **kw)
            except requests.RequestException as e:
                log.debug("GET %s failed: %s", url, e)
                if attempt == retries:
                    return None
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    retry_after = r.headers.get("Retry-After", "")
                    time.sleep(min(float(retry_after), 30) if retry_after.isdigit() else 5 * (attempt + 1))
                    continue
            return r
        return None

    def get_json(self, url: str, params=None, timeout: float = 20):
        r = self.get(url, params=params, timeout=timeout)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None


_default = None


def default_http(email: str = "") -> Http:
    global _default
    if _default is None or (email and _default.email != email):
        _default = Http(email)
    return _default
