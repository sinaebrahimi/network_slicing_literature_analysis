"""Unwrap tracking/redirect links in alert emails and recognise article URLs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

_REDIRECT_PARAMS = ("url", "u", "q", "target", "targeturl", "redirect", "redirect_url", "redirecturi",
                    "dest", "destination", "link", "r", "to", "goto")


def _proofpoint_v2(u: str) -> str:
    return unquote(u.replace("-", "%").replace("_", "/"))


def unwrap_url(url: str, max_depth: int = 5) -> str:
    """Follow redirect wrappers (Google Scholar, Outlook SafeLinks, Proofpoint,
    Elsevier/Mailchimp click tracking...) without any network access."""
    if not url:
        return url
    url = url.strip().replace("&amp;", "&")
    for _ in range(max_depth):
        p = urlparse(url)
        host = p.netloc.lower()
        qs = {k.lower(): v for k, v in parse_qs(p.query).items()}
        nxt = None
        if host.endswith("urldefense.proofpoint.com") and "u" in qs:
            nxt = _proofpoint_v2(qs["u"][0])
        elif host.endswith("urldefense.com") and "/v3/__" in p.path:
            m = re.search(r"/v3/__(.+?)__;", url)
            nxt = m.group(1) if m else None
        else:
            for key in _REDIRECT_PARAMS:
                for val in qs.get(key, []):
                    val = val.strip()
                    if re.match(r"^https?(://|%3A%2F%2F)", val, re.IGNORECASE):
                        nxt = unquote(val) if val.lower().startswith("http%3a") else val
                        break
                if nxt:
                    break
        if not nxt:
            # Encoded URL inside the path: click.notification.elsevier.com/CL0/https:%2F%2F.../1/...
            m = re.search(r"/(https?(?:%3A|:)(?:%2F%2F|//)[^/\s]+)", p.path + ("?" + p.query if p.query else ""),
                          re.IGNORECASE)
            if m and host and not p.path.lower().startswith(("/doi/", "/article", "/document")):
                nxt = unquote(m.group(1))
        if not nxt or nxt == url:
            return url
        url = nxt
    return url


ARTICLE_PATTERNS = [
    ("doi", re.compile(r"(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s?#\"<>]+)", re.IGNORECASE)),
    ("ieee", re.compile(r"ieeexplore\.ieee\.org/(?:abstract/|stamp/stamp\.jsp\?(?:tp=)?&?arnumber=|"
                        r"document/|xpl/articleDetails\.jsp\?arnumber=)(\d{5,})", re.IGNORECASE)),
    ("ieee", re.compile(r"ieeexplore\.ieee\.org/abstract/document/(\d{5,})", re.IGNORECASE)),
    ("pii", re.compile(r"sciencedirect\.com/science/article/(?:abs/|am/)?pii/(S?[0-9X]{15,17})", re.IGNORECASE)),
    ("doi", re.compile(r"dl\.acm\.org/doi/(?:abs/|full/|pdf/|epdf/|fullHtml/)?(10\.\d{4,9}/[^\s?#\"<>]+)",
                       re.IGNORECASE)),
    ("doi", re.compile(r"link\.springer\.com/(?:article|chapter|content/pdf)/(10\.\d{4,9}/[^\s?#\"<>]+?)(?:\.pdf)?(?=$|[?#])",
                       re.IGNORECASE)),
    ("nature", re.compile(r"nature\.com/articles/([a-z0-9\-\.]+?)(?:\.pdf)?(?=$|[?#/])", re.IGNORECASE)),
    ("doi", re.compile(r"onlinelibrary\.wiley\.com/doi/(?:abs/|full/|pdf/|epdf/|pdfdirect/)?(10\.\d{4,9}/[^\s?#\"<>]+)",
                       re.IGNORECASE)),
    ("doi", re.compile(r"tandfonline\.com/doi/(?:abs/|full/|pdf/)?(10\.\d{4,9}/[^\s?#\"<>]+)", re.IGNORECASE)),
    ("doi", re.compile(r"journals\.sagepub\.com/doi/(?:abs/|full/|pdf/)?(10\.\d{4,9}/[^\s?#\"<>]+)", re.IGNORECASE)),
    ("doi", re.compile(r"pubs\.acs\.org/doi/(?:abs/|full/|pdf/)?(10\.\d{4,9}/[^\s?#\"<>]+)", re.IGNORECASE)),
    ("arxiv", re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?",
                         re.IGNORECASE)),
    ("mdpi", re.compile(r"mdpi\.com/(\d{4}-\d{3}[\dX]/\d+/\d+/\d+)", re.IGNORECASE)),
    ("techrxiv", re.compile(r"techrxiv\.org/(?:doi/|articles/)", re.IGNORECASE)),
    ("researchgate", re.compile(r"researchgate\.net/publication/(\d+)", re.IGNORECASE)),
    ("semanticscholar", re.compile(r"semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})", re.IGNORECASE)),
    ("openreview", re.compile(r"openreview\.net/(?:forum|pdf)\?id=([A-Za-z0-9_\-]+)", re.IGNORECASE)),
]


def identify(url: str) -> dict:
    """Identifiers found in an article URL: doi / ieee (arnumber) / pii / arxiv / nature / is_pdf."""
    info = {"url": url}
    if not url:
        return info
    for kind, rx in ARTICLE_PATTERNS:
        m = rx.search(url)
        if m and kind not in info:
            val = m.group(1) if m.groups() else True
            if kind == "doi":
                val = unquote(val).rstrip(".,;)")
            if kind == "nature":
                info.setdefault("doi", f"10.1038/{val}")
            info[kind] = val
    path = urlparse(url).path.lower()
    info["is_pdf"] = path.endswith(".pdf") or "/pdf/" in path or "pdfft" in path or "stamppdf" in path \
        or "/content/pdf/" in path
    return info


def looks_like_article(url: str) -> bool:
    info = identify(url)
    return any(k in info for k in ("doi", "ieee", "pii", "arxiv", "nature", "mdpi", "techrxiv",
                                   "researchgate", "semanticscholar", "openreview")) or info.get("is_pdf", False)
