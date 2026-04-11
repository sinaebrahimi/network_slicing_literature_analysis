"""
Literature Catalog Builder v2
=============================
Scans PDF filenames, parses venue/year/author/title,
enriches with DOI + abstract from CrossRef / DOI page / Semantic Scholar.

Abstract retrieval chain:
  1. CrossRef (by title search → often has abstract in XML)
  2. CrossRef (by DOI → same)
  3. DOI landing page scrape (IEEE Xplore, ScienceDirect, MDPI, Springer, ACM...)
  4. Semantic Scholar fallback

Usage:
    python literature_catalog.py
"""

import os
import re
import csv
import time
import logging
import requests
from pathlib import Path
from html import unescape

# ============================================================
# CONFIGURATION
# ============================================================
LITERATURE_DIR = Path(
    r"C:\Users\ebrahimis\OneDrive - Coventry University\PhD\Literature"
)
OUTPUT_CSV = Path(os.path.expanduser("~/Documents/literature_catalog.csv"))
PUBLISHED_PREPRINTS_LOG = Path(os.path.expanduser("~/Documents/published_preprints.txt"))

SEMANTIC_SCHOLAR_DELAY = 0.4
CROSSREF_DELAY = 0.3
PAGE_FETCH_DELAY = 0.6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.expanduser("~/Documents/literature_catalog.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger(__name__)

VENUE_EXPANSIONS = {
    "TVT": "TVT", "TNSM": "TNSM", "TCOM": "TCOM", "TCCN": "TCCN",
    "COMST": "COMST", "MCOM": "MCOM", "OJCOMS": "OJCOMS",
    "JSYST": "JSYST", "JSAC": "JSAC", "TWC": "TWC", "TITS": "TITS",
    "TMC": "TMC", "ACCESS": "ACCESS", "LNET": "LNET", "TII": "TII",
    "IOTJ": "IOTJ", "CNSM": "CNSM", "NOMS": "NOMS", "ICC": "ICC",
    "GLOBECOM": "GLOBECOM", "VTC": "VTC", "WCNC": "WCNC",
    "PIMRC": "PIMRC", "NETSOFT": "NetSoft", "CAMAD": "CAMAD",
    "CSCN": "CSCN", "NOF": "NoF", "JCOMNET": "JCOMNET",
    "ELECTRONICS": "Electronics", "INFOCOM": "INFOCOM",
    "MNET": "MNET", "WCL": "WCL", "CL": "CL", "OJVT": "OJVT",
    "EUCNC": "EuCNC", "TII": "TII",
}

# Map publisher venue strings → acronym for reference keys
VENUE_KEYWORD_TO_ACRONYM = {
    "vehicular technology": "tvt",
    "network and service management": "tnsm",
    "ieee transactions on communications": "tcom",
    "cognitive communications": "tccn",
    "surveys and tutorials": "comst",
    "communications magazine": "mcom",
    "open journal of the communications": "ojcoms",
    "systems journal": "jsyst",
    "selected areas": "jsac",
    "wireless communications": "twc",
    "intelligent transportation": "tits",
    "mobile computing": "tmc",
    "ieee access": "access",
    "networking letters": "lnet",
    "industrial informatics": "tii",
    "internet of things": "iotj",
    "network softwarization": "netsoft",
    "computer networks": "jcomnet",
    "electronics (": "electronics",
    "electronics,": "electronics",
    "infocom": "infocom",
    "ieee network": "mnet",
    "6g summit": "eucnc",
    "eucnc": "eucnc",
    "globecom": "globecom",
    "icc ": "icc",
    "noms ": "noms",
    "cnsm ": "cnsm",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


# ============================================================
# FILENAME PARSING
# ============================================================
def parse_filename(fname: str) -> dict:
    stem = Path(fname).stem
    # Pattern: VENUE YEAR AUTHOR - TITLE  or  VENUE YEAR - TITLE
    m = re.match(
        r"^([A-Za-z_]+)\s*(\d{2,4})\s+"
        r"(?:([A-Za-z]+(?:\s[A-Za-z]+)?)\s*-\s*)?"
        r"(.+)$",
        stem,
    )
    if not m:
        parts = stem.split(" - ", 1)
        if len(parts) == 2:
            head, title_raw = parts
            tokens = head.strip().split()
            venue, year, author = tokens[0] if tokens else "", "", ""
            for t in tokens[1:]:
                if re.match(r"^\d{2,4}$", t):
                    year = t
                elif not year:
                    venue += " " + t
                else:
                    author = t
            return dict(venue_raw=venue.strip(), year=_norm_year(year),
                        author=author.strip(), title_raw=_clean(title_raw), filename=fname)
        return dict(venue_raw="", year="", author="", title_raw=stem, filename=fname)

    venue, year = m.group(1).strip(), m.group(2).strip()
    author = (m.group(3) or "").strip()
    title_raw = m.group(4).strip()
    if author and " - " not in stem:
        m2 = re.match(r"^([A-Za-z_]+)\s*(\d{2,4})\s+(.+)$", stem)
        if m2:
            venue, year, author = m2.group(1).strip(), m2.group(2).strip(), ""
            title_raw = m2.group(3).strip()
    return dict(venue_raw=venue, year=_norm_year(year),
                author=author, title_raw=_clean(title_raw), filename=fname)


def _norm_year(y):
    if not y: return ""
    return ("20" if int(y) <= 30 else "19") + y if len(y) == 2 else y

def _clean(t):
    t = t.replace("_", " ").strip()
    return re.sub(r"\s*-\s*Shortcut\s*$", "", t, flags=re.IGNORECASE)


# ============================================================
# SIMILARITY
# ============================================================
def _norm(s): return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

def _sim(a, b):
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0

def _best_match(query, items, title_fn):
    qn = _norm(query)
    scored = sorted(items, key=lambda x: -_sim(qn, _norm(title_fn(x))))
    return scored[0] if scored and _sim(qn, _norm(title_fn(scored[0]))) > 0.45 else (scored[0] if scored else None)


# ============================================================
# API FUNCTIONS
# ============================================================
def search_semantic_scholar(title, year=""):
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {"query": title, "limit": 5,
              "fields": "title,externalIds,abstract,year,authors,venue,publicationVenue"}
    if year: params["year"] = year
    try:
        r = SESSION.get(url, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(10)
            r = SESSION.get(url, params=params, timeout=15)
        if r.status_code != 200: return None
        papers = r.json().get("data", [])
        return _best_match(title, papers, lambda p: p.get("title", "")) if papers else None
    except: return None


def search_crossref(title):
    url = "https://api.crossref.org/works"
    params = {"query.title": title, "rows": 5}
    try:
        r = SESSION.get(url, params=params, timeout=15)
        if r.status_code != 200: return None
        items = r.json().get("message", {}).get("items", [])
        return _best_match(title, items, lambda x: (x.get("title") or [""])[0]) if items else None
    except: return None


def get_crossref_by_doi(doi):
    try:
        r = SESSION.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        return r.json().get("message") if r.status_code == 200 else None
    except: return None


def fetch_abstract_from_doi_page(doi):
    """Resolve DOI → publisher page → scrape abstract from HTML."""
    if not doi: return ""
    try:
        time.sleep(PAGE_FETCH_DELAY)
        r = SESSION.get(f"https://doi.org/{doi}", timeout=20, allow_redirects=True)
        if r.status_code != 200: return ""
        html = r.text

        # Meta tags (works for most publishers)
        for name, attr in [("citation_abstract", "name"), ("description", "name"),
                           ("og:description", "property"), ("DC.description", "name")]:
            text = _extract_meta(html, name, attr)
            if text and len(text) > 100:
                return _clean_abstract(text)

        # IEEE Xplore JSON blob
        if "ieeexplore.ieee.org" in r.url:
            m = re.search(r'"abstract"\s*:\s*"(.*?)"', html)
            if m:
                text = m.group(1).encode().decode("unicode_escape", errors="ignore")
                if len(text) > 80: return _clean_abstract(text)

        # ScienceDirect
        if "sciencedirect.com" in r.url:
            m = re.search(r'<div[^>]*class="abstract[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _strip_tags(m.group(1)).strip()
                if len(text) > 80: return _clean_abstract(text)

        # MDPI
        if "mdpi.com" in r.url:
            m = re.search(r'<div[^>]*class="art-abstract[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _strip_tags(m.group(1)).strip()
                if len(text) > 80: return _clean_abstract(text)

        # Generic patterns
        for pat in [r'<div[^>]*class="abstract"[^>]*>(.*?)</div>',
                    r'<section[^>]*id="abstract"[^>]*>(.*?)</section>',
                    r'<p[^>]*class="abstract"[^>]*>(.*?)</p>']:
            m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _strip_tags(m.group(1)).strip()
                if len(text) > 80: return _clean_abstract(text)

        return ""
    except Exception as e:
        log.debug(f"Page fetch error {doi}: {e}")
        return ""


def _extract_meta(html, name, attr="name"):
    for pat in [rf'<meta\s+{attr}="{name}"\s+content="(.*?)"',
                rf'<meta\s+content="(.*?)"\s+{attr}="{name}"',
                rf"<meta\s+{attr}='{name}'\s+content='(.*?)'",
                rf"<meta\s+content='(.*?)'\s+{attr}='{name}'"]:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m: return unescape(m.group(1)).strip()
    return ""

def _strip_tags(h): return re.sub(r"<[^>]+>", "", h)

def _clean_abstract(text):
    text = _strip_tags(unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^Abstract[\s:\-–—]*", "", text, flags=re.IGNORECASE)
    return text


# ============================================================
# REFERENCE KEY
# ============================================================
def make_ref_key(venue_acr, year, author):
    v = venue_acr.lower().strip()
    y = year[-2:] if len(year) >= 2 else year
    a = re.sub(r"[^a-z]", "", author.lower().strip())
    return f"{v}{y}{a}"


def guess_acronym_from_venue(venue_str):
    vl = venue_str.lower()
    for keyword, acr in VENUE_KEYWORD_TO_ACRONYM.items():
        if keyword in vl:
            return acr
    return ""


# ============================================================
# ENRICHMENT
# ============================================================
def enrich_entry(entry):
    title = entry["title_raw"]
    year = entry["year"]
    venue_upper = entry["venue_raw"].upper().replace(" ", "")
    is_preprint = venue_upper.startswith("PREP")

    result = {**entry, "title_official": "", "doi": "", "abstract": "",
              "first_author": entry["author"], "venue_acronym": entry["venue_raw"],
              "published_venue": ""}

    s2_abstract = ""

    # ---- Semantic Scholar ----
    time.sleep(SEMANTIC_SCHOLAR_DELAY)
    s2 = search_semantic_scholar(title, year)
    if s2:
        result["title_official"] = s2.get("title", "")
        s2_abstract = (s2.get("abstract") or "").replace("\n", " ").strip()
        ext = s2.get("externalIds") or {}
        if ext.get("DOI"):
            result["doi"] = ext["DOI"]
        if not result["first_author"] and s2.get("authors"):
            parts = s2["authors"][0].get("name", "").split()
            result["first_author"] = parts[-1] if parts else ""
        if is_preprint and s2.get("venue") and s2["venue"].lower() not in ("arxiv", "preprint", ""):
            result["published_venue"] = s2["venue"]

    # ---- CrossRef by title ----
    if not result["doi"]:
        time.sleep(CROSSREF_DELAY)
        cr = search_crossref(title)
        if cr:
            result["doi"] = cr.get("DOI", "")
            if not result["title_official"]:
                result["title_official"] = (cr.get("title") or [""])[0]
            if not result["first_author"] and cr.get("author"):
                result["first_author"] = cr["author"][0].get("family", "")
            ab = _clean_abstract(cr.get("abstract", ""))
            if ab and len(ab) > 80:
                result["abstract"] = ab
            if is_preprint:
                ct = cr.get("container-title", [])
                ct = ct[0] if isinstance(ct, list) and ct else (ct if isinstance(ct, str) else "")
                if ct and ct.lower() not in ("arxiv", ""):
                    result["published_venue"] = ct

    # ---- CrossRef by DOI for abstract ----
    if not result["abstract"] and result["doi"]:
        time.sleep(CROSSREF_DELAY)
        cr2 = get_crossref_by_doi(result["doi"])
        if cr2:
            ab = _clean_abstract(cr2.get("abstract", ""))
            if ab and len(ab) > 80:
                result["abstract"] = ab
            if not result["first_author"] and cr2.get("author"):
                result["first_author"] = cr2["author"][0].get("family", "")

    # ---- DOI page scrape ----
    if not result["abstract"] and result["doi"]:
        ab = fetch_abstract_from_doi_page(result["doi"])
        if ab and len(ab) > 80:
            result["abstract"] = ab

    # ---- S2 fallback abstract ----
    if not result["abstract"] and s2_abstract:
        result["abstract"] = s2_abstract

    # ---- Finalize ----
    if not result["title_official"]:
        result["title_official"] = title

    # Venue acronym + reference key
    if is_preprint and result.get("published_venue"):
        acr = guess_acronym_from_venue(result["published_venue"])
        if not acr:
            acr = entry["venue_raw"].lower()
        result["venue_acronym"] = acr.upper()
    else:
        result["venue_acronym"] = entry["venue_raw"]

    result["reference_key"] = make_ref_key(
        result["venue_acronym"], year, result["first_author"]
    )

    log.info(
        f"{'✓' if result['doi'] else '✗'} "
        f"[abs:{'yes' if result['abstract'] else 'no '}] "
        f"key={result['reference_key']:<28s} {entry['filename'][:65]}"
    )
    return result


# ============================================================
# MAIN
# ============================================================
def main():
    log.info(f"Scanning: {LITERATURE_DIR}")
    if not LITERATURE_DIR.exists():
        log.error(f"Directory not found: {LITERATURE_DIR}")
        return

    pdfs = sorted(f.name for f in LITERATURE_DIR.iterdir()
                  if f.suffix.lower() == ".pdf" and f.is_file())
    log.info(f"Found {len(pdfs)} PDF files")

    entries = [parse_filename(f) for f in pdfs]
    enriched = []

    for i, entry in enumerate(entries):
        log.info(f"[{i+1}/{len(entries)}] {entry['filename'][:80]}")
        try:
            enriched.append(enrich_entry(entry))
        except Exception as e:
            log.error(f"FAILED {entry['filename']}: {e}")
            enriched.append({
                **entry, "title_official": entry["title_raw"],
                "reference_key": make_ref_key(entry["venue_raw"], entry["year"], entry["author"]),
                "venue_acronym": entry["venue_raw"],
                "doi": "", "abstract": "", "first_author": entry["author"],
                "published_venue": "",
            })

    # Write CSV
    fields = ["title_official", "reference_key", "venue_acronym", "year",
              "doi", "abstract", "first_author", "published_venue", "filename", "title_raw"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(enriched)
    log.info(f"CSV → {OUTPUT_CSV}")

    # Preprints report
    pub = [e for e in enriched if e.get("published_venue")
           and e["venue_raw"].upper().startswith("PREP")]
    if pub:
        with open(PUBLISHED_PREPRINTS_LOG, "w", encoding="utf-8") as f:
            f.write("PREPRINTS NOW PUBLISHED\n" + "=" * 60 + "\n\n")
            for e in pub:
                f.write(f"File:   {e['filename']}\nTitle:  {e['title_official']}\n"
                        f"Now in: {e['published_venue']}\nKey:    {e['reference_key']}\n"
                        f"DOI:    {e['doi']}\n" + "-" * 60 + "\n")
        log.info(f"{len(pub)} preprints now published → {PUBLISHED_PREPRINTS_LOG}")

    n = len(enriched)
    log.info(f"Done! DOI: {sum(1 for e in enriched if e['doi'])}/{n}, "
             f"Abstract: {sum(1 for e in enriched if e['abstract'])}/{n}")


if __name__ == "__main__":
    main()
