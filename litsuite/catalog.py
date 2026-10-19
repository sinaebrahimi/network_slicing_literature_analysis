"""Build and maintain the CSV catalog of a PDF library.

* ``update_catalog`` adds rows for new PDFs, marks deleted PDFs as missing,
  follows renamed/moved files, and never overwrites what you edited by hand.
* ``create`` is simply an update starting from an empty catalog.
* ``fill_abstracts`` fills empty abstracts of existing rows.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os
import shutil
from pathlib import Path

from litsuite.http import Http, default_http
from litsuite.metadata import fetch_abstract, lookup_by_title
from litsuite.naming import parse_filename
from litsuite.textutils import normalize, title_key, title_similarity

log = logging.getLogger(__name__)

# Original v1 columns first (the dashboard and your spreadsheets rely on them).
BASE_FIELDS = ["title_official", "reference_key", "venue_acronym", "year", "doi", "abstract",
               "first_author", "filename"]
EXTRA_FIELDS = ["published_venue", "venue_full", "pub_type", "subfolder", "relative_path", "library",
                "status", "added_on", "source", "title_raw"]
FIELDS = BASE_FIELDS + EXTRA_FIELDS

AUTOSAVE_EVERY = 25
KEEP_BACKUPS = 15


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------
def read_catalog(path) -> tuple:
    """Return (rows, fieldnames). Handles UTF-8 (with/without BOM) and Excel's cp1252."""
    path = Path(path)
    if not path.exists():
        return [], list(FIELDS)
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.DictReader(text.splitlines(keepends=True))
    fieldnames = [f.replace("﻿", "").strip() for f in (reader.fieldnames or [])]
    reader.fieldnames = fieldnames
    rows = [{k: (v if v is not None else "") for k, v in r.items() if k is not None} for r in reader]
    return rows, fieldnames


def ordered_fields(existing=()) -> list:
    """Standard columns first, then any columns you added yourself (notes, rating...)."""
    return FIELDS + [f for f in existing if f and f not in FIELDS]


def backup_file(path: Path, backups_dir: Path) -> Path | None:
    if not path.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, dest)
    olds = sorted(backups_dir.glob(f"{path.stem}.*{path.suffix}"))
    for old in olds[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def write_catalog(path, rows, fieldnames=None) -> None:
    """Atomic write (UTF-8 with BOM so Excel shows accents correctly)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ordered_fields(fieldnames or [])
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Enrichment of one PDF
# ---------------------------------------------------------------------------
def make_ref_key(venue: str, year: str, author: str) -> str:
    import re

    v = re.sub(r"[^a-z0-9\-]", "", (venue or "").lower())
    y = year[-2:] if len(year or "") >= 2 else (year or "")
    a = re.sub(r"[^a-z]", "", normalize(author or "").replace(" ", ""))
    return f"{v}{y}{a}"


def enrich_entry(parsed: dict, http: Http | None = None, resolver=None, online: bool = True) -> dict:
    """Catalog row for one parsed filename, enriched with DOI/abstract when online."""
    http = http or default_http()
    title, year = parsed["title_raw"], parsed["year"]
    venue_raw = parsed["venue_raw"]
    is_prep = venue_raw.upper().replace(" ", "").startswith("PREP")
    row = {"title_official": title, "doi": "", "abstract": "", "first_author": parsed["author"],
           "venue_acronym": venue_raw, "year": year, "published_venue": "", "venue_full": "",
           "pub_type": "preprint" if is_prep else "", "title_raw": title}
    if online and title:
        meta = lookup_by_title(title, year, parsed["author"], http)
        if meta:
            row["title_official"] = meta.title or title
            row["doi"] = meta.doi
            row["abstract"] = meta.abstract
            row["venue_full"] = meta.venue_full
            if not is_prep and meta.pub_type:
                row["pub_type"] = meta.pub_type
            if not row["first_author"] and meta.first_author:
                row["first_author"] = meta.first_author
            if not year and meta.year:
                row["year"] = meta.year
            published = meta.venue_full and meta.pub_type not in ("preprint", "") \
                and "arxiv" not in meta.venue_full.lower()
            if is_prep and published:
                row["published_venue"] = meta.venue_full
                if resolver:
                    acr = resolver.resolve(meta.doi, meta.venue_full, meta.pub_type, meta.event_acronym)
                    if acr and acr != resolver.preprint_label:
                        row["venue_acronym"] = acr
        if not row["abstract"] and row["doi"]:
            row["abstract"], _ = fetch_abstract(row["doi"], "", http)
    row["reference_key"] = make_ref_key(row["venue_acronym"], row["year"], row["first_author"])
    return row


# ---------------------------------------------------------------------------
# Create / update
# ---------------------------------------------------------------------------
def _row_key(library: str, rel: str) -> tuple:
    return (library or "", (rel or "").replace("\\", "/").lower())


def _legacy_name(name: str) -> str:
    """Filename key for v1 rows (some hand-edited rows lack the .pdf extension)."""
    name = name.strip().lower()
    return name[:-4] if name.endswith(".pdf") else name


def _file_title_key(name: str) -> str:
    return title_key(parse_filename(name).title_raw)


def scan_libraries(storages, recursive: bool, include=(), exclude=()) -> list:
    files = []
    multi = len(storages) > 1
    for st in storages:
        for f in st.list_files(recursive=recursive, include=include, exclude=exclude):
            f.library = st.spec if multi else ""
            files.append(f)
    return files


def update_catalog(catalog_path, files, *, backups_dir=None, enrich: bool = True, prune: bool = False,
                   retry_incomplete: bool = False, http: Http | None = None, resolver=None,
                   source: str = "folder", today: str | None = None) -> dict:
    """Synchronise ``catalog_path`` with the list of LibraryFile objects in ``files``."""
    catalog_path = Path(catalog_path)
    backups_dir = Path(backups_dir) if backups_dir else catalog_path.parent / "backups"
    today = today or dt.date.today().isoformat()
    rows, fieldnames = read_catalog(catalog_path)
    if rows:
        backup_file(catalog_path, backups_dir)
    if resolver is None:
        from litsuite.venues import VenueResolver, learn_venue_codes

        resolver = VenueResolver(learned_codes=learn_venue_codes(rows))

    by_key = {}
    by_name: dict = {}
    for r in rows:
        if r.get("relative_path"):
            by_key[_row_key(r.get("library", ""), r["relative_path"])] = r
        else:
            by_name.setdefault(_legacy_name(r.get("filename") or ""), []).append(r)

    matched, new_files = set(), []
    for f in files:
        r = by_key.get(_row_key(f.library, f.relative_path))
        if r is None:
            legacy = by_name.get(_legacy_name(f.name))
            if legacy:
                r = legacy.pop(0)  # v1 rows only had a filename: adopt them
        if r is None:
            new_files.append(f)
            continue
        matched.add(id(r))
        r.update(relative_path=f.relative_path, subfolder=f.subfolder, library=f.library,
                 filename=f.name, status="present")
        r.setdefault("added_on", "")

    gone = [r for r in rows if id(r) not in matched]
    renamed = []
    if gone and new_files:
        gone_keys = {}
        for r in gone:
            k = title_key(r.get("title_raw") or parse_filename(r.get("filename", "")).title_raw)
            if k:
                gone_keys.setdefault(k, []).append(r)
        still_new = []
        for f in new_files:
            k = _file_title_key(f.name)
            cand = gone_keys.get(k)
            if not cand:
                # tolerate small edits to the title part of the filename
                for gk, rs in gone_keys.items():
                    if rs and title_similarity(gk, k) >= 0.92:
                        cand = rs
                        break
            if cand:
                r = cand.pop(0)
                renamed.append((r.get("relative_path") or r.get("filename"), f.relative_path))
                r.update(relative_path=f.relative_path, subfolder=f.subfolder, library=f.library,
                         filename=f.name, status="present")
                matched.add(id(r))
            else:
                still_new.append(f)
        new_files = still_new

    missing = [r for r in rows if id(r) not in matched]
    newly_missing = [r for r in missing if r.get("status") != "missing"]
    if prune:
        rows = [r for r in rows if id(r) in matched]
    else:
        for r in missing:
            r["status"] = "missing"

    def save():
        write_catalog(catalog_path, rows, fieldnames)

    added = []
    total = len(new_files)
    for i, f in enumerate(new_files, 1):
        parsed = parse_filename(f.name).as_dict()
        try:
            row = enrich_entry(parsed, http, resolver, online=enrich)
        except Exception as e:  # keep going: one bad lookup must not stop a 700-file run
            log.error("Lookup failed for %s: %s", f.name, e)
            row = enrich_entry(parsed, http, resolver, online=False)
        row.update(filename=f.name, relative_path=f.relative_path, subfolder=f.subfolder, library=f.library,
                   status="present", added_on=today, source=source)
        rows.append(row)
        added.append(row)
        log.info("[%d/%d] %s [abs:%s] key=%-24s %s", i, total, "✓" if row["doi"] else "✗",
                 "yes" if row["abstract"] else "no ", row["reference_key"], f.relative_path[:70])
        if i % AUTOSAVE_EVERY == 0:
            save()
            log.info("  -- autosaved (%d/%d) --", i, total)

    retried = 0
    if retry_incomplete and enrich:
        for r in rows:
            if r.get("status") == "missing" or (r.get("doi") and r.get("abstract")):
                continue
            parsed = parse_filename(r.get("filename", "")).as_dict()
            fresh = enrich_entry(parsed, http, resolver, online=True)
            for k in ("doi", "abstract", "venue_full", "pub_type"):
                if not r.get(k) and fresh.get(k):
                    r[k] = fresh[k]
            if not r.get("title_official"):
                r["title_official"] = fresh["title_official"]
            retried += 1

    save()
    return {
        "catalog": str(catalog_path), "total": len(rows), "files": len(files), "added": added,
        "missing": newly_missing, "renamed": renamed, "retried": retried, "pruned": prune,
        "duplicates": find_duplicates(rows),
        "published_preprints": [r for r in added if r.get("published_venue")],
    }


def fill_abstracts(catalog_path, output=None, backups_dir=None, http: Http | None = None, progress=None) -> dict:
    """Fill empty abstracts in place (or into ``output``)."""
    catalog_path = Path(catalog_path)
    rows, fieldnames = read_catalog(catalog_path)
    out_path = Path(output) if output else catalog_path
    if out_path.exists():
        backup_file(out_path, Path(backups_dir) if backups_dir else out_path.parent / "backups")
    todo = [r for r in rows if not (r.get("abstract") or "").strip() and r.get("status") != "missing"]
    stats: dict = {}
    filled = 0
    for i, r in enumerate(todo, 1):
        doi, title = (r.get("doi") or "").strip(), (r.get("title_official") or r.get("title_raw") or "").strip()
        if not doi and not title:
            continue
        ab, source = fetch_abstract(doi, title, http)
        stats[source] = stats.get(source, 0) + 1
        if ab:
            r["abstract"] = ab
            filled += 1
        log.info("[%d/%d] %s %s", i, len(todo), f"✓ [{source}]" if ab else "✗", doi or title[:60])
        if i % AUTOSAVE_EVERY == 0:
            write_catalog(out_path, rows, fieldnames)
    write_catalog(out_path, rows, fieldnames)
    return {"missing_before": len(todo), "filled": filled, "sources": stats, "output": str(out_path)}


def find_duplicates(rows) -> list:
    """Groups of rows that look like the same paper (same DOI or same title)."""
    groups: dict = {}
    for r in rows:
        if r.get("status") == "missing":
            continue
        keys = []
        if r.get("doi"):
            keys.append("doi:" + r["doi"].lower())
        tk = title_key(r.get("title_official") or r.get("title_raw") or "")
        if len(tk.split()) >= 3:
            keys.append("title:" + tk)
        for k in keys:
            groups.setdefault(k, []).append(r)
    seen, out = set(), []
    for k, rs in groups.items():
        files = tuple(sorted({r.get("relative_path") or r.get("filename") for r in rs}))
        if len(files) > 1 and files not in seen:
            seen.add(files)
            out.append(list(files))
    return out
