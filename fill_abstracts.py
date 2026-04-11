"""
Abstract Filler v1
==================
Reads litreview.csv, finds rows with empty abstract,
tries multiple sources to retrieve it using the DOI (and title as fallback).

Sources tried in order:
  1. CrossRef (by DOI) – many IEEE/Elsevier papers have abstracts
  2. IEEE Xplore API-like page scrape (via DOI redirect)
  3. Semantic Scholar (by DOI)
  4. OpenAlex (by DOI)
  5. DOI landing page scrape (meta tags, JSON blobs, HTML divs)

Only the 'abstract' column is modified. Rows with existing abstracts are untouched.

Usage:
    python fill_abstracts.py

Output:
    ~/Documents/litreview_filled.csv   (full file with abstracts filled in)
    ~/Documents/fill_abstracts.log     (log)
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
# Use the same folder as this script (works regardless of OneDrive redirection)
SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_CSV = SCRIPT_DIR / "litreview.csv"
OUTPUT_CSV = SCRIPT_DIR / "litreview_filled.csv"

# Delays between API calls (seconds)
CROSSREF_DELAY = 0.35
S2_DELAY = 0.4
OPENALEX_DELAY = 0.2
PAGE_DELAY = 0.7

# Auto-save progress every N rows (in case of crash)
AUTOSAVE_EVERY = 50

# DOIs that are not real DOIs (HAL, arXiv handles, etc.)
# We detect these and skip API lookups
NON_DOI_PREFIXES = ("hal-", "arxiv.", "10.48550/arxiv", "oai:")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            SCRIPT_DIR / "fill_abstracts.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger(__name__)

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
# CLEANING HELPERS
# ============================================================
def _strip_tags(h):
    return re.sub(r"<[^>]+>", "", h)

def _clean(text):
    if not text:
        return ""
    text = _strip_tags(unescape(str(text)))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^Abstract[\s:\-–—]*", "", text, flags=re.IGNORECASE)
    # Remove JATS XML tags that sometimes leak through CrossRef
    text = re.sub(r"</?jats:[^>]+>", "", text)
    return text.strip()

def _is_good(text):
    """Check abstract is real content, not a boilerplate snippet."""
    if not text or len(text) < 80:
        return False
    # Reject if it's just a journal description or cookie notice
    for bad in ["cookie", "this website", "javascript", "sign in", "log in",
                "access this article", "subscribe", "purchase"]:
        if bad in text.lower()[:100]:
            return False
    return True


# ============================================================
# SOURCE 1: CrossRef by DOI
# ============================================================
def try_crossref(doi):
    try:
        time.sleep(CROSSREF_DELAY)
        r = SESSION.get(f"https://api.crossref.org/works/{doi}", timeout=15)
        if r.status_code != 200:
            return ""
        msg = r.json().get("message", {})
        ab = _clean(msg.get("abstract", ""))
        if _is_good(ab):
            return ab
    except Exception as e:
        log.debug(f"CrossRef error {doi}: {e}")
    return ""


# ============================================================
# SOURCE 2: Semantic Scholar by DOI
# ============================================================
def try_semantic_scholar(doi):
    try:
        time.sleep(S2_DELAY)
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
        r = SESSION.get(url, params={"fields": "abstract"}, timeout=15)
        if r.status_code == 429:
            time.sleep(10)
            r = SESSION.get(url, params={"fields": "abstract"}, timeout=15)
        if r.status_code != 200:
            return ""
        ab = _clean(r.json().get("abstract", ""))
        if _is_good(ab):
            return ab
    except Exception as e:
        log.debug(f"S2 error {doi}: {e}")
    return ""


# ============================================================
# SOURCE 3: OpenAlex by DOI
# ============================================================
def try_openalex(doi):
    try:
        time.sleep(OPENALEX_DELAY)
        url = f"https://api.openalex.org/works/doi:{doi}"
        r = SESSION.get(url, timeout=15,
                        params={"select": "abstract_inverted_index"})
        if r.status_code != 200:
            return ""
        data = r.json()
        inv_idx = data.get("abstract_inverted_index")
        if not inv_idx:
            return ""
        # Reconstruct abstract from inverted index
        word_positions = []
        for word, positions in inv_idx.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort()
        ab = " ".join(w for _, w in word_positions)
        ab = _clean(ab)
        if _is_good(ab):
            return ab
    except Exception as e:
        log.debug(f"OpenAlex error {doi}: {e}")
    return ""


# ============================================================
# SOURCE 4: DOI landing page scrape
# ============================================================
def try_doi_page(doi):
    try:
        time.sleep(PAGE_DELAY)
        r = SESSION.get(f"https://doi.org/{doi}", timeout=25, allow_redirects=True)
        if r.status_code != 200:
            return ""
        html = r.text
        final_url = r.url.lower()

        # -- Meta tags (works for most publishers) --
        for name, attr in [
            ("citation_abstract", "name"),
            ("dc.description", "name"),
            ("DC.Description", "name"),
            ("description", "name"),
            ("og:description", "property"),
        ]:
            text = _extract_meta(html, name, attr)
            if _is_good(text):
                return _clean(text)

        # -- IEEE Xplore: JSON in page --
        if "ieeexplore.ieee.org" in final_url:
            # Try the global metadata JSON
            m = re.search(r'"abstract"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
            if m:
                text = m.group(1)
                try:
                    text = text.encode("utf-8").decode("unicode_escape")
                except Exception:
                    pass
                text = _clean(text)
                if _is_good(text):
                    return text

        # -- ScienceDirect --
        if "sciencedirect.com" in final_url:
            for pat in [
                r'<div[^>]*class="abstract[^"]*"[^>]*>(.*?)</div>',
                r'"abstract"\s*:\s*"((?:[^"\\]|\\.)*)"',
            ]:
                m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
                if m:
                    text = _clean(m.group(1))
                    if _is_good(text):
                        return text

        # -- MDPI --
        if "mdpi.com" in final_url:
            m = re.search(r'<div[^>]*class="art-abstract[^"]*"[^>]*>(.*?)</div>',
                          html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _clean(m.group(1))
                if _is_good(text):
                    return text

        # -- Springer --
        if "springer" in final_url or "link.springer" in final_url:
            m = re.search(r'<div[^>]*id="Abs[^"]*"[^>]*>(.*?)</div>',
                          html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _clean(m.group(1))
                if _is_good(text):
                    return text

        # -- ACM DL --
        if "dl.acm.org" in final_url:
            m = re.search(r'<div[^>]*class="abstractSection[^"]*"[^>]*>(.*?)</div>',
                          html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _clean(m.group(1))
                if _is_good(text):
                    return text

        # -- Generic fallback patterns --
        for pat in [
            r'<div[^>]*class="abstract"[^>]*>(.*?)</div>',
            r'<section[^>]*id="abstract"[^>]*>(.*?)</section>',
            r'<div[^>]*id="abstract"[^>]*>(.*?)</div>',
        ]:
            m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _clean(m.group(1))
                if _is_good(text):
                    return text

        return ""
    except Exception as e:
        log.debug(f"Page scrape error {doi}: {e}")
        return ""


def _extract_meta(html, name, attr="name"):
    for pat in [
        rf'<meta\s+{attr}="{name}"\s+content="(.*?)"',
        rf'<meta\s+content="(.*?)"\s+{attr}="{name}"',
        rf"<meta\s+{attr}='{name}'\s+content='(.*?)'",
        rf"<meta\s+content='(.*?)'\s+{attr}='{name}'",
        # Case-insensitive name match
        rf'(?i)<meta\s+{attr}="{name}"\s+content="(.*?)"',
    ]:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return unescape(m.group(1)).strip()
    return ""


# ============================================================
# ORCHESTRATOR: try all sources for one DOI
# ============================================================
def _is_real_doi(doi):
    """Check if DOI looks like a standard DOI (starts with 10.xxxx/)."""
    return bool(re.match(r"^10\.\d{4,}/", doi))


def _try_s2_by_title(title):
    """Fallback: search Semantic Scholar by title (for non-DOI papers)."""
    if not title:
        return ""
    try:
        time.sleep(S2_DELAY)
        r = SESSION.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": title, "limit": 3, "fields": "title,abstract"},
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(10)
            r = SESSION.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title, "limit": 3, "fields": "title,abstract"},
                timeout=15,
            )
        if r.status_code != 200:
            return ""
        papers = r.json().get("data", [])
        # Pick best title match
        qn = re.sub(r"[^a-z0-9 ]", "", title.lower())
        for p in papers:
            pn = re.sub(r"[^a-z0-9 ]", "", (p.get("title") or "").lower())
            sa, sb = set(qn.split()), set(pn.split())
            sim = len(sa & sb) / len(sa | sb) if sa and sb else 0
            if sim > 0.5:
                ab = _clean(p.get("abstract", ""))
                if _is_good(ab):
                    return ab
    except Exception:
        pass
    return ""


def fetch_abstract(doi, title=""):
    """Try sources in order, return first good abstract."""

    # Skip non-standard DOIs for DOI-based lookups
    real_doi = _is_real_doi(doi)

    if real_doi:
        # 1. CrossRef
        ab = try_crossref(doi)
        if ab:
            return ab, "crossref"

        # 2. Semantic Scholar by DOI
        ab = try_semantic_scholar(doi)
        if ab:
            return ab, "s2"

        # 3. OpenAlex
        ab = try_openalex(doi)
        if ab:
            return ab, "openalex"

        # 4. DOI page scrape
        ab = try_doi_page(doi)
        if ab:
            return ab, "page"

    # 5. Fallback: S2 title search (for fake DOIs or when DOI lookups all failed)
    if title:
        ab = _try_s2_by_title(title)
        if ab:
            return ab, "s2_title"

    return "", "none"


# ============================================================
# MAIN
# ============================================================
def main():
    log.info(f"Reading: {INPUT_CSV}")
    if not INPUT_CSV.exists():
        log.error(f"File not found: {INPUT_CSV}")
        log.info("Make sure litreview.csv is in the same folder as this script.")
        return

    # Read with fallback encoding
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            with open(INPUT_CSV, encoding=enc) as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                rows = list(reader)
            log.info(f"Read {len(rows)} rows (encoding: {enc})")
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        log.error("Could not read CSV with any encoding")
        return

    log.info(f"Columns: {fieldnames}")
    already = sum(1 for r in rows if r.get("abstract", "").strip())
    missing = sum(1 for r in rows if not r.get("abstract", "").strip())
    log.info(f"Already have abstract: {already}, Missing: {missing}")

    filled = 0
    failed = 0
    sources_stats = {"crossref": 0, "s2": 0, "openalex": 0, "page": 0, "s2_title": 0, "none": 0}

    # Build list of indices that need work
    todo = [(i, row) for i, row in enumerate(rows) if not row.get("abstract", "").strip()]
    log.info(f"Processing {len(todo)} rows (skipping {already} that already have abstracts)")

    for idx, (i, row) in enumerate(todo, 1):
        doi = row.get("doi", "").strip()
        title = row.get("title_official", "").strip()

        if not doi and not title:
            log.info(f"[{idx}/{len(todo)}] SKIP no DOI/title: {row.get('filename','')[:60]}")
            sources_stats["none"] += 1
            failed += 1
            continue

        log.info(f"[{idx}/{len(todo)}] Fetching: {doi or title[:50]}"
                 + (" (non-standard DOI, title search)" if doi and not _is_real_doi(doi) else ""))
        ab, source = fetch_abstract(doi, title)

        if ab:
            row["abstract"] = ab
            filled += 1
            sources_stats[source] += 1
            log.info(f"  ✓ [{source}] {len(ab)} chars  |  Progress: {filled}/{idx} succeeded ({filled*100//idx}%)")
        else:
            sources_stats["none"] += 1
            failed += 1
            log.info(f"  ✗ not found  |  Progress: {filled}/{idx} succeeded, {failed} failed")

        # Autosave
        if (idx % AUTOSAVE_EVERY == 0):
            _save(rows, fieldnames)
            log.info(f"  -- autosaved at {idx}/{len(todo)} ({filled} filled) --")

    # Final save
    _save(rows, fieldnames)

    log.info("=" * 60)
    log.info(f"DONE. Filled {filled}/{missing} missing abstracts.")
    log.info(f"Sources: {sources_stats}")
    log.info(f"Total with abstract now: {already + filled}/{len(rows)}")
    log.info(f"Output: {OUTPUT_CSV}")


def _save(rows, fieldnames):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()