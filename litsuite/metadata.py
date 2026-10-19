"""Paper metadata from free scholarly APIs (no API keys needed).

* CrossRef         DOI, official title, authors, venue, type, often the abstract
* Semantic Scholar title search, abstract, open-access PDF, preprint -> published venue
* OpenAlex         abstract (inverted index), open-access PDF
* arXiv            preprint metadata
* DOI landing page abstract from publisher HTML (IEEE, ScienceDirect, MDPI, Springer, ACM...)
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from html import unescape

from litsuite.http import Http, default_http
from litsuite.textutils import clean_abstract, clean_text, is_good_abstract, title_similarity

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.75   # title similarity needed to accept a search hit

CROSSREF_TYPES = {
    "journal-article": "journal", "proceedings-article": "conference", "posted-content": "preprint",
    "book-chapter": "book", "book": "book", "monograph": "book", "edited-book": "book",
    "reference-entry": "book", "dissertation": "thesis", "report": "report", "standard": "standard",
}


@dataclass
class PaperMeta:
    title: str = ""
    authors: list = field(default_factory=list)   # family names, in order
    year: str = ""
    venue_full: str = ""
    event_acronym: str = ""
    doi: str = ""
    abstract: str = ""
    pub_type: str = ""          # journal | conference | preprint | book | other
    publisher: str = ""
    url: str = ""
    pdf_urls: list = field(default_factory=list)   # known open-access PDF links
    arxiv_id: str = ""
    published_venue: str = ""   # for preprints that have since been published
    sources: list = field(default_factory=list)

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    def merge(self, other: "PaperMeta") -> "PaperMeta":
        """Fill empty fields from ``other``."""
        for k, v in asdict(other).items():
            cur = getattr(self, k)
            if isinstance(cur, list):
                for item in v:
                    if item not in cur:
                        cur.append(item)
            elif not cur and v:
                setattr(self, k, v)
        return self


def _family(name: str) -> str:
    name = (name or "").strip()
    if "," in name:
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.IGNORECASE)
    return doi.rstrip(".,;)")


def is_real_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.\d{4,}/", doi or ""))


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------
def _from_crossref(item: dict) -> PaperMeta:
    title = clean_text((item.get("title") or [""])[0])
    subtitle = clean_text((item.get("subtitle") or [""])[0]) if item.get("subtitle") else ""
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"
    year = ""
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break
    ct = item.get("container-title") or []
    venue = clean_text(ct[0] if isinstance(ct, list) and ct else ct if isinstance(ct, str) else "")
    event = item.get("event") or {}
    meta = PaperMeta(
        title=title,
        authors=[a.get("family") or _family(a.get("name", "")) for a in item.get("author") or []
                 if a.get("family") or a.get("name")],
        year=year,
        venue_full=venue or clean_text(event.get("name", "")),
        event_acronym=clean_text(event.get("acronym", "")) if isinstance(event, dict) else "",
        doi=item.get("DOI", ""),
        abstract=clean_abstract(item.get("abstract", "")),
        pub_type=CROSSREF_TYPES.get(item.get("type", ""), "other"),
        publisher=item.get("publisher", ""),
        url=item.get("URL", ""),
        sources=["crossref"],
    )
    if item.get("subtype") == "preprint":
        meta.pub_type = "preprint"
    for link in item.get("link") or []:
        if "pdf" in (link.get("content-type") or "") and link.get("intended-application") != "text-mining":
            meta.pdf_urls.append(link.get("URL"))
    if not is_good_abstract(meta.abstract):
        meta.abstract = ""
    return meta


def crossref_by_doi(doi: str, http: Http | None = None):
    http = http or default_http()
    data = http.get_json(f"https://api.crossref.org/works/{normalize_doi(doi)}")
    return _from_crossref(data["message"]) if data and data.get("message") else None


def crossref_search(title: str, author: str = "", year: str = "", http: Http | None = None,
                    threshold: float = MATCH_THRESHOLD):
    http = http or default_http()
    params = {"query.bibliographic": title, "rows": 5}
    if author:
        params["query.author"] = author
    data = http.get_json("https://api.crossref.org/works", params=params)
    items = ((data or {}).get("message") or {}).get("items") or []
    best, best_sim = None, 0.0
    for it in items:
        sim = title_similarity(title, (it.get("title") or [""])[0])
        if sim > best_sim:
            best, best_sim = it, sim
    if best is None or best_sim < threshold:
        return None
    meta = _from_crossref(best)
    if str(year).isdigit() and meta.year.isdigit() and abs(int(meta.year) - int(year)) > 2 and best_sim < 0.95:
        return None
    return meta


def crossref_by_pii(pii: str, http: Http | None = None):
    """ScienceDirect article IDs (PII) are registered as alternative IDs in CrossRef."""
    http = http or default_http()
    data = http.get_json("https://api.crossref.org/works", params={"filter": f"alternative-id:{pii}", "rows": 1})
    items = ((data or {}).get("message") or {}).get("items") or []
    return _from_crossref(items[0]) if items else None


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------
S2_FIELDS = "title,externalIds,abstract,year,authors,venue,publicationVenue,openAccessPdf,publicationTypes"


def _from_s2(p: dict) -> PaperMeta:
    ext = p.get("externalIds") or {}
    venue = p.get("venue") or ((p.get("publicationVenue") or {}).get("name") or "")
    meta = PaperMeta(
        title=clean_text(p.get("title", "")),
        authors=[_family(a.get("name", "")) for a in p.get("authors") or [] if a.get("name")],
        year=str(p.get("year") or ""),
        venue_full=venue,
        doi=ext.get("DOI", "") or "",
        abstract=clean_abstract(p.get("abstract") or ""),
        arxiv_id=ext.get("ArXiv", "") or "",
        sources=["semanticscholar"],
    )
    types = p.get("publicationTypes") or []
    if "JournalArticle" in types:
        meta.pub_type = "journal"
    elif "Conference" in types:
        meta.pub_type = "conference"
    oa = (p.get("openAccessPdf") or {}).get("url")
    if oa:
        meta.pdf_urls.append(oa)
    if not is_good_abstract(meta.abstract):
        meta.abstract = ""
    return meta


def s2_search(title: str, year: str = "", http: Http | None = None, threshold: float = MATCH_THRESHOLD):
    http = http or default_http()
    params = {"query": title, "limit": 5, "fields": S2_FIELDS}
    if year:
        params["year"] = year
    data = http.get_json("https://api.semanticscholar.org/graph/v1/paper/search", params=params)
    papers = (data or {}).get("data") or []
    best, best_sim = None, 0.0
    for p in papers:
        sim = title_similarity(title, p.get("title") or "")
        if sim > best_sim:
            best, best_sim = p, sim
    return _from_s2(best) if best is not None and best_sim >= threshold else None


def s2_by_id(paper_id: str, http: Http | None = None):
    """paper_id like 'DOI:10.1109/...' or 'ARXIV:2401.01234'."""
    http = http or default_http()
    data = http.get_json(f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}", params={"fields": S2_FIELDS})
    return _from_s2(data) if data and data.get("title") else None


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------
def _inverted_to_text(inv: dict) -> str:
    pos = sorted((p, w) for w, ps in (inv or {}).items() for p in ps)
    return " ".join(w for _, w in pos)


def openalex_by_doi(doi: str, http: Http | None = None):
    http = http or default_http()
    params = {"mailto": http.email} if http.email else None
    data = http.get_json(f"https://api.openalex.org/works/doi:{normalize_doi(doi)}", params=params)
    if not data:
        return None
    src = ((data.get("primary_location") or {}).get("source") or {})
    meta = PaperMeta(
        title=clean_text(data.get("title") or ""),
        authors=[_family((a.get("author") or {}).get("display_name", "")) for a in data.get("authorships") or []],
        year=str(data.get("publication_year") or ""),
        venue_full=src.get("display_name") or "",
        doi=normalize_doi(data.get("doi") or ""),
        abstract=clean_abstract(_inverted_to_text(data.get("abstract_inverted_index"))),
        sources=["openalex"],
    )
    for loc in [data.get("best_oa_location") or {}] + (data.get("locations") or []):
        if loc and loc.get("pdf_url") and loc["pdf_url"] not in meta.pdf_urls:
            meta.pdf_urls.append(loc["pdf_url"])
    if not is_good_abstract(meta.abstract):
        meta.abstract = ""
    return meta


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------
def arxiv_by_id(arxiv_id: str, http: Http | None = None):
    http = http or default_http()
    r = http.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id})
    if r is None or r.status_code != 200:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        entry = ET.fromstring(r.text).find("a:entry", ns)
    except ET.ParseError:
        return None
    if entry is None or entry.find("a:title", ns) is None:
        return None
    published = entry.findtext("a:published", "", ns)
    doi = entry.findtext("arxiv:doi", "", ns)
    journal = entry.findtext("arxiv:journal_ref", "", ns)
    meta = PaperMeta(
        title=clean_text(entry.findtext("a:title", "", ns)),
        authors=[_family(a.findtext("a:name", "", ns)) for a in entry.findall("a:author", ns)],
        year=published[:4],
        venue_full="arXiv",
        doi=doi,
        abstract=clean_abstract(entry.findtext("a:summary", "", ns)),
        pub_type="preprint" if not doi else "",
        arxiv_id=arxiv_id,
        published_venue=journal,
        url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_urls=[f"https://arxiv.org/pdf/{arxiv_id}"],
        sources=["arxiv"],
    )
    return meta


# ---------------------------------------------------------------------------
# Publisher landing page (DOI redirect) abstract scraping
# ---------------------------------------------------------------------------
def _extract_meta(html: str, name: str, attr: str = "name") -> str:
    for pat in (rf'<meta\s+{attr}="{name}"\s+content="(.*?)"', rf'<meta\s+content="(.*?)"\s+{attr}="{name}"',
                rf"<meta\s+{attr}='{name}'\s+content='(.*?)'", rf"<meta\s+content='(.*?)'\s+{attr}='{name}'"):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return unescape(m.group(1)).strip()
    return ""


def abstract_from_html(html: str, final_url: str = "") -> str:
    for name, attr in (("citation_abstract", "name"), ("dc.description", "name"), ("DC.Description", "name"),
                       ("description", "name"), ("og:description", "property")):
        text = clean_abstract(_extract_meta(html, name, attr))
        if is_good_abstract(text):
            return text
    url = final_url.lower()
    patterns = []
    if "ieeexplore.ieee.org" in url:
        m = re.search(r'"abstract"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if m:
            text = m.group(1)
            try:
                text = text.encode("utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                pass
            text = clean_abstract(text)
            if is_good_abstract(text):
                return text
    if "sciencedirect.com" in url:
        patterns += [r'<div[^>]*class="abstract[^"]*"[^>]*>(.*?)</div>']
    if "mdpi.com" in url:
        patterns += [r'<div[^>]*class="art-abstract[^"]*"[^>]*>(.*?)</div>']
    if "springer" in url or "nature.com" in url:
        patterns += [r'<div[^>]*id="Abs[^"]*"[^>]*>(.*?)</div>']
    if "dl.acm.org" in url:
        patterns += [r'<div[^>]*class="abstractSection[^"]*"[^>]*>(.*?)</div>']
    patterns += [r'<div[^>]*class="abstract"[^>]*>(.*?)</div>', r'<section[^>]*id="abstract"[^>]*>(.*?)</section>',
                 r'<div[^>]*id="abstract"[^>]*>(.*?)</div>', r'<p[^>]*class="abstract"[^>]*>(.*?)</p>']
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            text = clean_abstract(m.group(1))
            if is_good_abstract(text):
                return text
    return ""


def doi_page_abstract(doi: str, http: Http | None = None) -> str:
    http = http or default_http()
    r = http.get(f"https://doi.org/{normalize_doi(doi)}", timeout=25, allow_redirects=True)
    if r is None or r.status_code != 200:
        return ""
    return abstract_from_html(r.text, r.url)


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------
def fetch_abstract(doi: str, title: str = "", http: Http | None = None):
    """Return (abstract, source) trying CrossRef, S2, OpenAlex, DOI page, S2 title search."""
    http = http or default_http()
    if is_real_doi(doi):
        for source, fn in (("crossref", lambda: crossref_by_doi(doi, http)),
                           ("s2", lambda: s2_by_id(f"DOI:{doi}", http)),
                           ("openalex", lambda: openalex_by_doi(doi, http))):
            meta = fn()
            if meta and meta.abstract:
                return meta.abstract, source
        ab = doi_page_abstract(doi, http)
        if ab:
            return ab, "page"
    if title:
        meta = s2_search(title, http=http, threshold=0.5)
        if meta and meta.abstract:
            return meta.abstract, "s2_title"
    return "", "none"


def lookup_by_title(title: str, year: str = "", author: str = "", http: Http | None = None):
    """Find a paper by its title: Semantic Scholar first, then CrossRef (as in v1)."""
    http = http or default_http()
    meta = s2_search(title, year, http)
    cr = None
    if meta and meta.doi and is_real_doi(meta.doi):
        cr = crossref_by_doi(meta.doi, http)
    if not cr:
        cr = crossref_search(title, author, year, http)
    if meta and cr:
        # CrossRef is authoritative for venue/type; S2 often has the abstract.
        return cr.merge(meta)
    return meta or cr


def complete(meta: PaperMeta, http: Http | None = None) -> PaperMeta:
    """Fill missing abstract / OA links / authors for a paper with a DOI."""
    http = http or default_http()
    if meta.doi and is_real_doi(meta.doi):
        if not meta.title or not meta.authors or not meta.venue_full:
            cr = crossref_by_doi(meta.doi, http)
            if cr:
                meta.merge(cr)
        if not meta.abstract:
            oa = openalex_by_doi(meta.doi, http)
            if oa:
                meta.merge(oa)
        if not meta.abstract:
            meta.abstract = doi_page_abstract(meta.doi, http)
    return meta
