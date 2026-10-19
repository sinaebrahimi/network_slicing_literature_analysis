"""Title abbreviations ("Network Slicing" -> "NS").

Abbreviations live in plain YAML files (``phrase: SHORT``) so they are easy to
audit and edit. The same table is used in two directions:

* ``abbreviate``: when building a filename for a newly downloaded paper.
* ``expand``:     when matching alert titles against your keywords, so that a
                  keyword "deep reinforcement learning" also matches "DRL".

``learn_abbreviations`` mines the abbreviations you already use by comparing
your filenames with the official titles in the catalog.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import yaml

_SEP = r"[\s_\-]+"
ISOLATION_TOKEN = "zzsepzz"
_WORD_SPLIT = re.compile(r"[\s_\-]+")


def _phrase_regex(phrase: str) -> re.Pattern:
    words = [w for w in _WORD_SPLIT.split(phrase.strip()) if w]
    body = _SEP.join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


class Abbreviator:
    """Applies a phrase -> short-form table to text."""

    def __init__(self, mapping: dict | None = None):
        self.mapping: dict = {}
        for phrase, short in (mapping or {}).items():
            if phrase is None or short is None:
                continue
            phrase, short = str(phrase).strip(), str(short).strip()
            if phrase and short and phrase.lower() != short.lower():
                self.mapping[phrase] = short
        # Longest phrases first so "Multi-Agent Deep Reinforcement Learning"
        # wins over "Deep Reinforcement Learning".
        ordered = sorted(self.mapping.items(),
                         key=lambda kv: (-len(_WORD_SPLIT.split(kv[0])), -len(kv[0])))
        self._forward = [(_phrase_regex(p), s) for p, s in ordered]
        expansions: dict = {}
        for phrase, short in ordered:
            expansions.setdefault(short, phrase)
        self._backward = [
            (re.compile(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])"), p)
            for s, p in sorted(expansions.items(), key=lambda kv: -len(kv[0]))
        ]

    def __len__(self):
        return len(self.mapping)

    def __bool__(self):
        return bool(self.mapping)

    def abbreviate(self, text: str) -> str:
        for rx, short in self._forward:
            text = rx.sub(short, text)
        return text

    def expand(self, text: str, isolate: bool = False) -> str:
        """Replace short forms (case-sensitive, whole tokens) by their phrase.

        ``isolate`` fences each expansion with a separator token so that a
        keyword cannot match across its edge ("RAN Slicing" must not become a
        match for "network slicing").
        """
        for rx, phrase in self._backward:
            text = rx.sub(f" {ISOLATION_TOKEN} {phrase} {ISOLATION_TOKEN} " if isolate else phrase, text)
        return text

    def explain(self, text: str) -> list:
        """Which rules fire on ``text`` (for the audit/preview command)."""
        hits = []
        for rx, short in self._forward:
            for m in rx.finditer(text):
                hits.append((m.group(0), short))
            text = rx.sub(short, text)
        return hits


def load_abbreviation_file(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and "abbreviations" in data and isinstance(data["abbreviations"], dict):
        data = data["abbreviations"]
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected 'Phrase: SHORT' lines")
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}


def load_abbreviations(files: Iterable = (), inline: dict | None = None) -> Abbreviator:
    """Merge abbreviation files in order; later files and ``inline`` win.

    A phrase mapped to an empty value disables an entry inherited from an
    earlier file, e.g. ``Resource Allocation: ""`` keeps it spelled out.
    """
    merged: dict = {}
    for f in files or ():
        merged.update(load_abbreviation_file(f))
    merged.update({str(k): ("" if v is None else str(v)) for k, v in (inline or {}).items()})
    return Abbreviator({k: v for k, v in merged.items() if v})


# ---------------------------------------------------------------------------
# Learning abbreviations from an existing library
# ---------------------------------------------------------------------------
_ABBR_TOKEN = re.compile(r"^(?=(?:.*[A-Z]){2})[A-Z0-9][A-Za-z0-9\-]{1,9}$")
_DIGIT_WORDS = {"2": ("to", "two"), "4": ("for", "four")}


def _initials_match(words: list, letters: str) -> bool:
    """True if the words' initials spell ``letters`` (digits may stand for to/for)."""
    if len(words) != len(letters):
        return False
    for word, letter in zip(words, letters):
        wl = word.lower()
        if letter.isdigit():
            if not (wl in _DIGIT_WORDS.get(letter, ()) or wl.startswith(letter)):
                return False
        elif not wl.startswith(letter.lower()):
            return False
    return True


def _find_expansion(abbr: str, official: str):
    letters = abbr.replace("-", "")
    spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[A-Za-z0-9]+", official)]
    n = len(letters)
    for i in range(len(spans) - n + 1):
        window = spans[i:i + n]
        if _initials_match([w[2] for w in window], letters):
            phrase = official[window[0][0]:window[-1][1]]
            if len(window) >= 2 and not any(ch in phrase for ch in ":;,()"):
                return phrase
    return None


def _filename_title(filename: str) -> str:
    stem = Path(filename).stem
    m = re.search(r"\s+-\s*|\s*-\s+", stem)
    return stem[m.end():] if m else stem


_SMALL_WORDS = {"of", "to", "and", "for", "in", "on", "the", "a", "an", "at", "by", "with"}


def _title_word(word: str, index: int) -> str:
    if word.isupper():
        return word
    if index > 0 and word.lower() in _SMALL_WORDS:
        return word.lower()
    return word[:1].upper() + word[1:]


def learn_abbreviations(rows: Iterable[dict], min_count: int = 2) -> list:
    """Suggest ``(phrase, SHORT, count, example)`` from filenames vs official titles.

    Example: the file ``... Dynamic_RA_Enabler_for_Resilient_NS.pdf`` whose
    official title is "... Dynamic Resource Allocation Enabler for Resilient
    Network Slicing" yields ``Resource Allocation -> RA`` and
    ``Network Slicing -> NS``.
    """
    counts: Counter = Counter()
    surface: dict = defaultdict(Counter)
    examples: dict = {}
    for row in rows:
        official = (row.get("title_official") or "").strip()
        filename = (row.get("filename") or "").strip()
        if not official or not filename:
            continue
        ftitle = _filename_title(filename).replace("_", " ")
        official_words = {w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", official)}
        for tok in set(re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", ftitle)):
            tok = tok.strip("-")
            if not _ABBR_TOKEN.match(tok) or tok.lower() in official_words:
                continue
            phrase = _find_expansion(tok, official)
            if not phrase:
                continue
            key = (phrase.lower().replace("-", " "), tok)
            counts[key] += 1
            surface[key][phrase] += 1
            examples.setdefault(key, filename)
    out = []
    for key, n in counts.most_common():
        if n < min_count:
            continue
        phrase = " ".join(_title_word(w, i) for i, w in
                          enumerate(surface[key].most_common(1)[0][0].split(" ")))
        out.append((phrase, key[1], n, examples[key]))
    return out


def write_suggestions(suggestions: list, path) -> Path:
    """Write learned abbreviations as a commented YAML file for review."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Abbreviations learned from your existing filenames (litsuite abbrev learn).",
        "# Review them, then copy the ones you want into your project's abbreviation file",
        "# (or list this file under 'abbreviations:' in settings.yaml).",
        "# Format:  Phrase: SHORT        # times seen | example file",
    ]
    for phrase, short, n, example in suggestions:
        safe_example = example.replace("#", "")
        lines.append(f"{yaml.safe_dump({phrase: short}, allow_unicode=True).strip():<55} # {n}x | {safe_example}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
