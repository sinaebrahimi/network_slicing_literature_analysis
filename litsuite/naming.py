"""Parse and build PDF filenames that follow the library naming convention.

Convention (configurable in settings.yaml under ``naming``)::

    VENUE YY Lastname - Title_With_Underscores.pdf
    MNET 25 Fryganiotis - Model_Predictive_Control-Based_Dynamic_Resource_Allocation_Enabler_for_Resilient_NS.pdf

Older files without an author are also understood::

    ICC 23 - 5G_Network_Slicing_for_Mission_Critical_Use_Cases.pdf
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

from litsuite.abbreviations import Abbreviator
from litsuite.textutils import ascii_fold, clean_text

# Characters that are illegal in Windows / macOS / Linux filenames.
_ILLEGAL = r'[<>:"/\\|?*\x00-\x1f]'

DEFAULT_NAMING = {
    # Placeholders: {venue} {yy} {yyyy} {author} {title}
    "template": "{venue} {yy} {author} - {title}",
    "template_no_author": "{venue} {yy} - {title}",
    "word_separator": "_",          # "_" (IEEE style) or " "
    "colon_replacement": "",        # "Title: Subtitle" -> "Title_Subtitle"; use "-" for "Title-_Subtitle"
    "ascii_only": True,             # Gonçalves -> Goncalves
    "max_title_length": 150,        # characters, cut at a word boundary
    "max_filename_length": 200,
    "preprint_label": "PREP",
    "unknown_venue_label": "MISC",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class ParsedName:
    venue_raw: str
    year: str
    author: str
    title_raw: str
    filename: str

    def as_dict(self) -> dict:
        return dict(venue_raw=self.venue_raw, year=self.year, author=self.author,
                    title_raw=self.title_raw, filename=self.filename)


_VENUE = r"(?P<venue>\S+|\S[^\d]{0,25}?\S)"
_HEAD_WITH_SEP = re.compile(
    _VENUE + r"\s+(?P<year>\d{4}|\d{2})(?:\s+(?P<author>[^\s-].*?))?(?:\s+-\s*|\s*-\s+)(?P<title>.+)$"
)
_HEAD_NO_SEP = re.compile(_VENUE + r"\s+(?P<year>\d{4}|\d{2})\s+(?P<title>.+)$")
# "VENUE YY - Surname - Title" (author written after the first dash)
_LEADING_SURNAME = re.compile(r"^(?P<author>[A-Z][a-z'\-]+(?:-[A-Z][a-z'\-]+)?)\s+-\s+(?P<title>.+)$")


def normalize_year(y: str) -> str:
    if not y:
        return ""
    if len(y) == 2:
        pivot = (_dt.date.today().year + 1) % 100
        return ("20" if int(y) <= pivot else "19") + y
    return y


def _clean_title(t: str) -> str:
    t = t.replace("_", " ")
    t = re.sub(r"\s*-\s*Shortcut\s*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


def parse_filename(fname: str) -> ParsedName:
    """Split a convention filename into venue / year / author / title."""
    stem = Path(fname).stem.strip()
    m = _HEAD_WITH_SEP.match(stem)
    if m:
        author = (m.group("author") or "").strip()
        title = m.group("title")
        if author and (len(author.split()) > 3 or len(author) > 40):
            # "VENUE YY Long Title - Subtitle": there is no author, the dash is in the title.
            title = f"{author} - {title}"
            author = ""
        if not author:
            lead = _LEADING_SURNAME.match(title)
            if lead:
                author, title = lead.group("author"), lead.group("title")
        author = re.sub(r"\(.*?\)", "", author.split(",")[0]).strip()
        return ParsedName(m.group("venue"), normalize_year(m.group("year")), author,
                          _clean_title(title), fname)
    m = _HEAD_NO_SEP.match(stem)
    if m:
        return ParsedName(m.group("venue"), normalize_year(m.group("year")), "",
                          _clean_title(m.group("title")), fname)
    return ParsedName("", "", "", _clean_title(stem), fname)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
def sanitize_component(text: str, cfg: dict) -> str:
    text = clean_text(text)
    if cfg.get("ascii_only", True):
        text = ascii_fold(text)
    text = text.replace(":", cfg.get("colon_replacement", ""))
    text = re.sub(_ILLEGAL, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_title(title: str, abbreviator: Abbreviator | None = None, cfg: dict | None = None) -> str:
    cfg = {**DEFAULT_NAMING, **(cfg or {})}
    title = clean_text(title)
    if abbreviator:
        title = abbreviator.abbreviate(title)
    title = sanitize_component(title, cfg)
    sep = cfg.get("word_separator", "_")
    if sep != " ":
        title = re.sub(r"\s+", sep, title)
        title = re.sub(re.escape(sep) + r"{2,}", sep, title)
    max_len = int(cfg.get("max_title_length") or 0)
    if max_len and len(title) > max_len:
        cut = title[:max_len]
        boundary = max(cut.rfind(sep), cut.rfind(" "))
        title = cut[:boundary] if boundary > max_len // 2 else cut
    return title.strip(" ._-")


def author_lastname(author: str, cfg: dict | None = None) -> str:
    cfg = {**DEFAULT_NAMING, **(cfg or {})}
    author = sanitize_component(author or "", cfg)
    return author.replace(" ", "") if cfg.get("word_separator", "_") != " " else author


def build_filename(venue: str, year, author: str, title: str,
                   abbreviator: Abbreviator | None = None, cfg: dict | None = None) -> str:
    """Return ``VENUE YY Author - Title.pdf`` for a paper's metadata."""
    cfg = {**DEFAULT_NAMING, **(cfg or {})}
    year = str(year or "").strip()
    yyyy = year if len(year) == 4 else normalize_year(year) if year else ""
    venue = sanitize_component(venue or cfg["unknown_venue_label"], cfg).replace(" ", "")
    author = author_lastname(author, cfg)
    title_part = format_title(title, abbreviator, cfg)
    template = cfg["template"] if author else cfg["template_no_author"]
    name = template.format(venue=venue, yy=yyyy[-2:] if yyyy else "XX", yyyy=yyyy or "XXXX",
                           author=author, title=title_part)
    name = re.sub(r"\s+", " ", name).strip()
    max_len = int(cfg.get("max_filename_length") or 0)
    if max_len and len(name) > max_len - 4:
        name = name[: max_len - 4].rstrip(" ._-")
    return name + ".pdf"


def unique_name(name: str, existing) -> str:
    """Append (2), (3)... when ``name`` already exists (case-insensitive)."""
    taken = {e.lower() for e in existing}
    if name.lower() not in taken:
        return name
    stem, ext = name[:-4], name[-4:]
    i = 2
    while f"{stem} ({i}){ext}".lower() in taken:
        i += 1
    return f"{stem} ({i}){ext}"
