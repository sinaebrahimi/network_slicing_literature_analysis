"""Turn a paper's venue into the short name used in filenames (TNSM, ICC, jcomnet...).

Resolution order for a new paper:

1. Preprint (arXiv, TechRxiv, SSRN, bioRxiv...)  -> ``preprint_label`` (PREP)
2. Venue codes learned from your own catalog: every DOI carries a stable
   journal/conference code (10.1109/TNSM..., 10.1016/j.comnet..., 10.1007/s10922...)
   and your catalog already says which short name you use for it.
3. Your venue map (config/venues/*.yaml): "venue name" -> SHORT
4. Publisher DOI conventions (IEEE codes such as TNSM/ICC/GLOBECOM, MDPI journal names...)
5. An acronym in the venue name itself, "... Conference on Communications (ICC)"
6. Initials of the venue name ("Journal of Network and Systems Management" -> JNSM)
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import yaml

from litsuite.textutils import normalize

PREPRINT_DOI_PREFIXES = ("10.48550", "10.36227", "10.2139", "10.1101", "10.20944", "10.31219", "10.31224")
# DOIs of general-purpose repositories say nothing about the venue.
REPOSITORY_DOI_PREFIXES = ("10.5281", "10.6084", "10.17605", "10.5061")
PREPRINT_NAMES = ("arxiv", "techrxiv", "biorxiv", "medrxiv", "ssrn", "preprints", "research square",
                  "authorea", "chemrxiv", "osf preprints", "psyarxiv", "socarxiv", "engrxiv")

# Default "code -> short name" style per DOI prefix when you have not used a
# venue before. "{CODE}" = upper-case code, "{code}" = as in the DOI.
DEFAULT_DOI_STYLES = {
    "10.1109": "{CODE}",   # IEEE: 10.1109/TNSM.2024..., 10.1109/ICC45041.2023...
    "10.3390": "{code}",   # MDPI: 10.3390/electronics12030567
}

# Codes that differ from the name researchers use.
DEFAULT_CODE_MAP = {
    "10.3390/s": "sensors",
    "10.3390/app": "applsci",
    "10.3390/fi": "futureinternet",
    "10.3390/ijerph": "ijerph",
    "10.1038/s41586": "Nature",
    "10.1038/s41467": "NatCommun",
    "10.1038/s41598": "srep",
    "10.1038/s41928": "NatElectron",
    "10.1038/s42256": "NatMachIntell",
    "10.1126/science": "Science",
    "10.1073/pnas": "PNAS",
}

_NAME_STOPWORDS = {
    "of", "the", "and", "on", "in", "for", "to", "a", "an", "&", "de", "la", "le", "et", "fur", "und",
    "ieee", "acm", "ifip", "usenix", "proceedings", "proc", "annual", "international", "intl",
    "st", "nd", "rd", "th",
}


_VENUE_FILLER = {"proceedings", "proc", "the", "of", "annual", "international", "intl", "ieee", "acm", "ifip",
                 "usenix", "and", "on", "in", "for", "a", "an"}


def venue_key(name: str) -> str:
    """Comparable form of a venue name: no years, ordinals, 'Proceedings of the', IEEE/ACM, filler words."""
    toks = [t for t in normalize(name).split()
            if t not in _VENUE_FILLER and not re.fullmatch(r"\d+(st|nd|rd|th)?", t)]
    return " ".join(toks)


def doi_venue_code(doi: str):
    """Extract a stable per-venue key from a DOI, e.g. ``10.1109/TNSM``.

    Returns ``None`` when the DOI carries no venue information (ACM, Hindawi...).
    """
    if not doi:
        return None
    doi = doi.strip().lower()
    m = re.match(r"^(10\.\d{4,9})/(.+)$", doi)
    if not m:
        return None
    prefix, suffix = m.groups()
    patterns = [
        r"^j\.([a-z]+)\.",                 # Elsevier: j.comnet.2023...
        r"^(s\d{5})-",                     # Springer / Nature / BMC: s10922-023-...
        r"^([a-z][a-z0-9\-]*?)\d*\.\d{4}", # IEEE: tnsm.2024..., icc45041.2023..., 6gnet58894.2023
        r"^([a-z]+[a-z\-]*?)\.\d",         # Wiley / IET / Science / PNAS: dac.5432, ett.4567
        r"^([a-z]+)\d",                    # MDPI: electronics12030567
        r"^([a-z]+)\.",                    # other journal-code DOIs (mcom.001.2200...)
        r"^([a-z][a-z0-9]*)$",
    ]
    if suffix.startswith(("978-", "979-")) or prefix in ("10.1145", "10.1155") \
            or prefix in REPOSITORY_DOI_PREFIXES:
        return None
    for pat in patterns:
        mm = re.match(pat, suffix)
        if mm:
            code = mm.group(1).strip("-")
            if code and not code.isdigit():
                return f"{prefix}/{code}"
    return None


def is_preprint(doi: str = "", venue_name: str = "", pub_type: str = "") -> bool:
    if pub_type == "preprint":
        return True
    if doi and doi.lower().startswith(PREPRINT_DOI_PREFIXES):
        return True
    vn = (venue_name or "").lower()
    return any(p in vn for p in PREPRINT_NAMES)


def acronym_from_name(name: str) -> str:
    """Acronym written in the venue name, or the initials of its significant words."""
    if not name:
        return ""
    # "2023 IEEE International Conference on Communications (ICC)" -> ICC
    for m in re.finditer(r"\(([A-Za-z][A-Za-z0-9\-&]{1,15})(?:\s*'?\d{2,4})?\)", name):
        cand = m.group(1)
        if sum(c.isupper() for c in cand) >= 2:
            return cand
    # "ICC 2023 - IEEE International Conference on Communications" -> ICC
    m = re.match(r"^\s*([A-Z][A-Za-z0-9\-]{1,14}[A-Z0-9])\s*'?\d{2,4}\b", name)
    if m and sum(c.isupper() for c in m.group(1)) >= 2:
        return m.group(1)
    cleaned = re.sub(r"\(.*?\)", " ", name)
    cleaned = re.sub(r"\b\d+(st|nd|rd|th)?\b", " ", cleaned)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-']*", cleaned) if w.lower() not in _NAME_STOPWORDS]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return "".join(w[0].upper() for w in words)


def load_venue_files(files: Iterable) -> list:
    """Load ``venue name: SHORT`` YAML files into a list of (normalized name, SHORT)."""
    entries = []
    for f in files or ():
        p = Path(f)
        if not p.exists():
            continue
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and isinstance(data.get("venues"), dict):
            data = data["venues"]
        for name, short in (data or {}).items():
            if name and short:
                entries.append((str(name), str(short)))
    return entries


class VenueResolver:
    def __init__(self, venue_map: Iterable = (), learned_codes: dict | None = None,
                 doi_styles: dict | None = None, preprint_label: str = "PREP"):
        self.preprint_label = preprint_label
        self.learned = dict(learned_codes or {})
        self.doi_styles = {**DEFAULT_DOI_STYLES, **(doi_styles or {})}
        self.code_map = {**DEFAULT_CODE_MAP}
        self._names = []
        self._codes = {}
        for name, short in venue_map:
            if re.match(r"^10\.\d{4,9}/", name):
                self._codes[name.lower()] = short
            elif name.startswith("~"):
                key = venue_key(name[1:])
                rx = re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])")
                self._names.append((key, rx, short))
            else:
                self._names.append((venue_key(name), None, short))
        self._names.sort(key=lambda t: -len(t[0]))

    def resolve(self, doi: str = "", venue_name: str = "", pub_type: str = "",
                acronym_hint: str = "") -> str:
        """``acronym_hint``: e.g. CrossRef's event acronym ("MobiCom '23")."""
        if is_preprint(doi, venue_name, pub_type):
            return self.preprint_label
        code = doi_venue_code(doi)
        if code:
            if code in self._codes:
                return self._codes[code]
            if code in self.learned:
                return self.learned[code]
        if venue_name:
            nv = venue_key(venue_name)
            for key, rx, short in self._names:
                if nv == key or (rx is not None and rx.search(nv)):
                    return short
        if code:
            if code in self.code_map:
                return self.code_map[code]
            prefix, raw = code.split("/", 1)
            style = self.doi_styles.get(prefix)
            if style:
                return style.replace("{CODE}", raw.upper()).replace("{code}", raw)
        if acronym_hint:
            hint = re.sub(r"\s*'?\d{2,4}$", "", acronym_hint.strip())
            if hint and len(hint) <= 20:
                return hint.replace(" ", "")
        return acronym_from_name(venue_name)


def learn_venue_codes(rows: Iterable[dict]) -> dict:
    """Map DOI venue codes to the short names already used in a catalog."""
    votes = defaultdict(Counter)
    for r in rows:
        acr = (r.get("venue_acronym") or "").strip()
        doi = r.get("doi") or ""
        code = doi_venue_code(doi)
        if not acr or not code or acr.upper().startswith("PREP") or is_preprint(doi):
            continue
        votes[code][acr] += 1
    return {code: c.most_common(1)[0][0] for code, c in votes.items()}
