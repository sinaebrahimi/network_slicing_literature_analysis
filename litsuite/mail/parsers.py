"""Extract candidate papers (title, link, authors, snippet) from alert emails.

Built-in alert sources (extend or override them under ``alert_sources`` in settings.yaml):

==============  ==========================================  ===============
source          sender(s)                                   parser
==============  ==========================================  ===============
google_scholar  scholaralerts-noreply@google.com            google_scholar
sciencedirect   @notification.elsevier.com, @elsevier.com   generic
ieee            @ieee.org (subject must look like an alert) generic
acm             @acm.org  (subject must look like an alert) generic
springer        @springernature.com, @springer.com, @nature.com  generic
wiley           @wiley.com                                  generic
mdpi            @mdpi.com                                   generic
arxiv           no-reply@arxiv.org                          arxiv
semantic_scholar @semanticscholar.org                       generic
==============  ==========================================  ===============
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from litsuite.mail.base import MailMessage, sender_matches
from litsuite.mail.links import identify, looks_like_article, unwrap_url
from litsuite.textutils import clean_text, normalize

ALERT_SUBJECT_WORDS = ["alert", "table of contents", "toc", "new content", "new articles", "new results",
                       "latest articles", "saved search", "early access", "new issue", "just published",
                       "recommended", "feed"]

BUILTIN_SOURCES = {
    "google_scholar": {"senders": ["scholaralerts-noreply@google.com"], "parser": "google_scholar"},
    "sciencedirect": {"senders": ["@notification.elsevier.com", "@sciencedirect.com", "@elsevier.com"],
                      "parser": "generic"},
    "ieee": {"senders": ["@ieee.org"], "parser": "generic", "subjects": ALERT_SUBJECT_WORDS},
    "acm": {"senders": ["@acm.org", "@hq.acm.org"], "parser": "generic", "subjects": ALERT_SUBJECT_WORDS},
    "springer": {"senders": ["@springernature.com", "@springer.com", "@nature.com", "@biomedcentral.com"],
                 "parser": "generic", "subjects": ALERT_SUBJECT_WORDS},
    "wiley": {"senders": ["@wiley.com"], "parser": "generic", "subjects": ALERT_SUBJECT_WORDS},
    "mdpi": {"senders": ["@mdpi.com"], "parser": "generic", "subjects": ALERT_SUBJECT_WORDS},
    "arxiv": {"senders": ["no-reply@arxiv.org", "@arxiv.org"], "parser": "arxiv"},
    "semantic_scholar": {"senders": ["@semanticscholar.org"], "parser": "generic"},
}

_NON_TITLE = re.compile(
    r"^(view|read|download|access|see|full text|full-text|pdf|html|abstract|unsubscribe|manage|"
    r"privacy|terms|cookie|contact|help|log ?in|sign ?in|share|save|cite|related|more|click|"
    r"open access|table of contents|view all|view article|read more|learn more|preview)\b",
    re.IGNORECASE,
)


@dataclass
class AlertCandidate:
    title: str
    url: str = ""
    pdf_url: str = ""
    authors: list = field(default_factory=list)
    authors_line: str = ""
    venue_line: str = ""
    year: str = ""
    snippet: str = ""
    doi: str = ""
    arxiv_id: str = ""
    ieee_arnumber: str = ""
    pii: str = ""
    source: str = ""
    message_id: str = ""
    message_subject: str = ""
    message_date: str = ""
    account: str = ""

    @property
    def key(self) -> str:
        return normalize(self.title)

    def absorb(self, other: "AlertCandidate") -> None:
        """Merge information about the same paper seen in another email."""
        for f in ("url", "pdf_url", "authors_line", "venue_line", "year", "snippet", "doi", "arxiv_id",
                  "ieee_arnumber", "pii"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))
        if not self.authors and other.authors:
            self.authors = other.authors


def _apply_ids(c: AlertCandidate, url: str) -> None:
    info = identify(url)
    c.doi = c.doi or info.get("doi", "")
    c.arxiv_id = c.arxiv_id or info.get("arxiv", "")
    c.ieee_arnumber = c.ieee_arnumber or info.get("ieee", "")
    c.pii = c.pii or info.get("pii", "")
    if info.get("is_pdf") and not c.pdf_url:
        c.pdf_url = url


def _family_names(authors_line: str) -> list:
    names = []
    for part in re.split(r",|\band\b", authors_line):
        part = re.sub(r"\bet al\.?", "", part.replace("…", "").replace("...", "")).strip()
        if not part or len(part) > 60:
            continue
        names.append(part.split()[-1])
    return names


# ---------------------------------------------------------------------------
# Google Scholar
# ---------------------------------------------------------------------------
def parse_google_scholar(msg: MailMessage) -> list:
    soup = BeautifulSoup(msg.html or "", "html.parser")
    out = []
    anchors = soup.select("a.gse_alrt_title") or [
        a for a in soup.find_all("a", href=True) if "scholar_url?" in a["href"] and a.find_parent("h3")]
    for a in anchors:
        title = clean_text(a.get_text(" ", strip=True))
        if not title:
            continue
        url = unwrap_url(a.get("href", ""))
        c = AlertCandidate(title=title, url=url)
        h3 = a.find_parent("h3") or a
        marker = h3.find("span")
        if marker and "[PDF]" in marker.get_text():
            c.pdf_url = url
        _apply_ids(c, url)
        meta_div = h3.find_next_sibling("div")
        if meta_div is not None and "gse_alrt_sni" not in (meta_div.get("class") or []):
            line = clean_text(meta_div.get_text(" ", strip=True))
            authors, _, venue = line.partition(" - ")
            c.authors_line, c.venue_line = authors.strip(), venue.strip()
            c.authors = _family_names(c.authors_line)
            years = re.findall(r"\b(?:19|20)\d{2}\b", venue)
            if years:
                c.year = years[-1]
            meta_div = meta_div.find_next_sibling("div")
        if meta_div is not None and "gse_alrt_sni" in (meta_div.get("class") or []):
            c.snippet = clean_text(meta_div.get_text(" ", strip=True))
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Generic publisher alert (ScienceDirect, IEEE, ACM, Springer, Wiley, MDPI...)
# ---------------------------------------------------------------------------
def _block_text(tag, title: str) -> str:
    block = tag
    for _ in range(4):
        if block.parent is None:
            break
        block = block.parent
        if block.name in ("td", "tr", "li", "div", "p", "table"):
            text = clean_text(block.get_text(" ", strip=True))
            if len(text) > len(title) + 20:
                return text.replace(title, "", 1).strip(" -|·")[:600]
    return ""


def parse_generic(msg: MailMessage) -> list:
    soup = BeautifulSoup(msg.html or "", "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text(" ", strip=True))
        words = title.split()
        if len(words) < 4 or len(words) > 60 or _NON_TITLE.match(title) or title.startswith("http"):
            continue
        url = unwrap_url(a["href"])
        if not looks_like_article(url):
            continue
        key = normalize(title)
        if key in seen:
            continue
        seen.add(key)
        c = AlertCandidate(title=title, url=url)
        _apply_ids(c, url)
        context = _block_text(a, title)
        if context:
            c.snippet = context
            years = re.findall(r"\b(?:19|20)\d{2}\b", context)
            if years:
                c.year = years[-1]
        out.append(c)
    if not out and msg.text and not msg.html:
        out = parse_plain_links(msg.text)
    return out


def parse_plain_links(text: str) -> list:
    """Plain-text alerts: 'Title' followed by an article URL on the next line(s)."""
    out = []
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        for url in re.findall(r"https?://\S+", line):
            url = unwrap_url(url.rstrip(">).,"))
            if not looks_like_article(url):
                continue
            title = ""
            for j in range(i - 1, max(-1, i - 4), -1):
                if len(lines[j].split()) >= 4 and not lines[j].startswith("http"):
                    title = lines[j]
                    break
            if title:
                c = AlertCandidate(title=clean_text(title), url=url)
                _apply_ids(c, url)
                out.append(c)
    return out


# ---------------------------------------------------------------------------
# arXiv listing digests (plain text)
# ---------------------------------------------------------------------------
def parse_arxiv(msg: MailMessage) -> list:
    text = msg.text or BeautifulSoup(msg.html or "", "html.parser").get_text("\n")
    out = []
    for block in re.split(r"\n-{20,}\n", text):
        m_id = re.search(r"arXiv:(\d{4}\.\d{4,5})", block)
        m_title = re.search(r"Title:\s*(.+?)\n(?=Authors?:)", block, re.DOTALL)
        if not m_id or not m_title:
            continue
        title = re.sub(r"\s+", " ", m_title.group(1)).strip()
        m_auth = re.search(r"Authors?:\s*(.+?)\n(?=[A-Z][a-z\-]+:|\\\\)", block, re.DOTALL)
        authors_line = re.sub(r"\s+", " ", m_auth.group(1)).strip() if m_auth else ""
        parts = re.split(r"\n\\\\\s*\n", block)
        snippet = re.sub(r"\s+", " ", parts[2]).strip()[:600] if len(parts) > 2 else ""
        aid = m_id.group(1)
        out.append(AlertCandidate(
            title=title, url=f"https://arxiv.org/abs/{aid}", pdf_url=f"https://arxiv.org/pdf/{aid}",
            authors_line=authors_line, authors=_family_names(authors_line), arxiv_id=aid,
            venue_line="arXiv", snippet=snippet, year=f"20{aid[:2]}"))
    return out


PARSERS = {"google_scholar": parse_google_scholar, "generic": parse_generic, "arxiv": parse_arxiv}


def all_sources(settings_sources: dict | None = None) -> dict:
    sources = {k: dict(v) for k, v in BUILTIN_SOURCES.items()}
    for name, cfg in (settings_sources or {}).items():
        sources[name] = {**sources.get(name, {"parser": "generic"}), **(cfg or {})}
    return sources


def match_source(msg: MailMessage, sources: dict, enabled=None):
    """Name of the alert source that sent ``msg`` (or None)."""
    for name, cfg in sources.items():
        if enabled is not None and name not in enabled:
            continue
        if not sender_matches(msg.sender, cfg.get("senders")):
            continue
        subjects = cfg.get("subjects")
        if subjects and not any(w.lower() in (msg.subject or "").lower() for w in subjects):
            continue
        return name
    return None


def parse_message(msg: MailMessage, source: str, sources: dict) -> list:
    parser = PARSERS.get((sources.get(source) or {}).get("parser", "generic"), parse_generic)
    cands = parser(msg)
    if not cands and parser is not parse_generic:
        cands = parse_generic(msg)   # layout changed? fall back to generic link extraction
    date = msg.date.date().isoformat() if msg.date else ""
    for c in cands:
        c.source, c.message_id, c.message_subject = source, msg.id, msg.subject
        c.message_date, c.account = date, msg.account
    return cands
