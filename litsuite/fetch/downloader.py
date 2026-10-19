"""Get the PDF of a paper: open access first, then your institutional browser session."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from litsuite.fetch.browser import BrowserSession, is_pdf
from litsuite.fetch.publishers import pdf_urls, publisher_for, publishers, via_proxy
from litsuite.http import Http, default_http
from litsuite.metadata import PaperMeta, is_real_doi, normalize_doi

log = logging.getLogger(__name__)

MIN_PDF_BYTES = 5_000


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    url: str = ""
    method: str = ""      # open-access | direct | browser
    tried: list = None
    note: str = ""


def unpaywall_pdfs(doi: str, email: str, http: Http) -> list:
    if not email or not is_real_doi(doi):
        return []
    data = http.get_json(f"https://api.unpaywall.org/v2/{normalize_doi(doi)}", params={"email": email})
    if not data:
        return []
    out = []
    for loc in [data.get("best_oa_location") or {}] + (data.get("oa_locations") or []):
        url = (loc or {}).get("url_for_pdf")
        if url and url not in out:
            out.append(url)
    return out


class PaperDownloader:
    def __init__(self, access: dict, email: str = "", http: Http | None = None, browser_factory=None):
        self.access = access or {}
        self.email = email
        self.http = http or default_http(email)
        self.table = publishers(self.access.get("publishers"))
        self._browser_factory = browser_factory or (lambda: BrowserSession.from_access(self.access))
        self._browser = None
        self._browser_error = ""

    # -- browser lifecycle (opened lazily, shared across papers) -------------
    def browser(self):
        if self._browser_error:
            raise RuntimeError(self._browser_error)
        if self._browser is None:
            try:
                self._browser = self._browser_factory().__enter__()
            except Exception as e:   # remember it: do not retry the launch for every paper
                self._browser_error = str(e)
                raise RuntimeError(self._browser_error) from e
        return self._browser

    def close(self):
        if self._browser is not None:
            self._browser.__exit__(None, None, None)
            self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- strategies ------------------------------------------------------------
    def _http_pdf(self, url: str, dest: Path) -> bool:
        r = self.http.get(url, timeout=60, allow_redirects=True, headers={"Accept": "application/pdf,*/*"})
        if r is None or r.status_code != 200 or not is_pdf(r.content[:1024]) or len(r.content) < MIN_PDF_BYTES:
            return False
        dest.write_bytes(r.content)
        return True

    def candidate_urls(self, meta: PaperMeta, ids: dict, direct_pdf: str = "") -> tuple:
        """(open-access URLs, publisher URLs needing a signed-in session)."""
        oa = []
        if direct_pdf:
            oa.append(direct_pdf)
        oa += [u for u in meta.pdf_urls if u]
        if meta.arxiv_id or ids.get("arxiv_id"):
            oa.append(f"https://arxiv.org/pdf/{meta.arxiv_id or ids['arxiv_id']}")
        if self.access.get("open_access_first", True):
            oa += unpaywall_pdfs(meta.doi, self.email, self.http)
        key = publisher_for(meta.doi, ids.get("url", ""), self.table)
        paywalled = pdf_urls(self.table[key], {**ids, "doi": meta.doi or ids.get("doi", "")}) if key else []
        if meta.doi:
            paywalled.append(f"https://doi.org/{meta.doi}")
        elif ids.get("url"):
            paywalled.append(ids["url"])
        dedup = lambda xs: list(dict.fromkeys(x for x in xs if x))  # noqa: E731
        return dedup(oa), dedup(paywalled)

    def download(self, meta: PaperMeta, dest: Path, ids: dict | None = None, direct_pdf: str = "",
                 use_browser: bool = True) -> DownloadResult:
        ids = dict(ids or {})
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        oa, paywalled = self.candidate_urls(meta, ids, direct_pdf)
        tried = []
        for url in oa:
            tried.append(url)
            if self._http_pdf(url, dest):
                return DownloadResult(True, dest, url, "open-access", tried)
        method = (self.access.get("method") or "browser").lower()
        if use_browser and method == "browser" and paywalled:
            try:
                browser = self.browser()
            except RuntimeError as e:
                return DownloadResult(False, None, "", "", tried, str(e))
            for url in paywalled:
                url = via_proxy(url, self.access)
                tried.append(url)
                try:
                    if browser.fetch_pdf(url, dest) and dest.stat().st_size >= MIN_PDF_BYTES:
                        return DownloadResult(True, dest, url, "browser", tried)
                except Exception as e:
                    log.debug("browser fetch %s failed: %s", url, e)
            # Open-access copies sometimes need a browser too (cookie walls, JS redirects).
            for url in oa:
                try:
                    if browser.fetch_pdf(url, dest) and dest.stat().st_size >= MIN_PDF_BYTES:
                        return DownloadResult(True, dest, url, "browser", tried)
                except Exception:
                    pass
        if dest.exists():
            dest.unlink()
        note = "no open-access copy found"
        if paywalled and not (use_browser and method == "browser"):
            note += "; browser downloads disabled"
        elif paywalled:
            note += "; publisher refused the PDF (not signed in? run: litsuite login publisher)"
        return DownloadResult(False, None, "", "", tried, note)
