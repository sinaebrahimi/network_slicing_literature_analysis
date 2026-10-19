"""Small text helpers shared by every module (cleaning, normalising, matching)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from html import unescape

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "for", "to", "on", "with", "by", "from",
    "is", "are", "was", "were", "be", "been", "this", "that", "these", "those", "it",
    "its", "we", "our", "as", "at", "can", "has", "have", "using", "based", "via",
    "through", "into", "between", "over", "under", "also", "such", "more", "most",
    "each", "both", "which", "their", "than", "not", "but", "will", "may", "about",
    "towards", "toward", "vs", "versus", "within", "without", "how", "what", "when",
    "where", "why", "do", "does", "new", "novel", "paper", "propose", "proposed",
    "approach", "method", "results", "show", "study", "analysis", "however", "two",
    "first", "scheme", "framework", "consider", "existing", "different", "provide",
    "present", "work", "several", "one", "well", "demonstrate", "demonstrates",
    "evaluate", "evaluated", "achieve", "achieved", "compared", "various", "use",
    "used",
}

# Characters that ASCII folding (NFKD) does not decompose on its own.
_SPECIAL_FOLD = {
    "ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "þ": "th", "Þ": "Th",
    "–": "-", "—": "-", "‐": "-", "‑": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
    "…": "...",
}


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def clean_text(text) -> str:
    """Unescape HTML entities, drop tags (including JATS), collapse whitespace."""
    if not text:
        return ""
    text = unescape(str(text))
    text = re.sub(r"</?jats:[^>]+>", " ", text)
    text = strip_tags(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_abstract(text) -> str:
    text = clean_text(text)
    text = re.sub(r"^(Abstract|Summary)[\s:.\-–—]*", "", text, flags=re.IGNORECASE)
    return text.strip()


def is_good_abstract(text: str) -> bool:
    """Real abstract content, not a cookie banner or paywall blurb."""
    if not text or len(text) < 80:
        return False
    head = text.lower()[:100]
    for bad in ("cookie", "this website", "javascript", "sign in", "log in",
                "access this article", "subscribe", "purchase"):
        if bad in head:
            return False
    return True


def ascii_fold(text: str) -> str:
    if not text:
        return ""
    text = "".join(_SPECIAL_FOLD.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def normalize(text: str) -> str:
    """Lowercase ASCII words separated by single spaces (punctuation removed)."""
    text = ascii_fold(clean_text(text)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def tokens(text: str) -> list:
    return normalize(text).split()


def stem(word: str) -> str:
    """Very light suffix stripping so that slice/slicing/slices all match."""
    w = word.lower()
    if len(w) <= 4:
        return w
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def stem_tokens(text: str) -> list:
    return [stem(t) for t in tokens(text)]


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def title_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two titles (robust to punctuation/case)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    jac = jaccard(na.split(), nb.split())
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return max(jac, ratio)


def title_key(title: str) -> str:
    """Key used to detect the same paper under slightly different titles."""
    return " ".join(t for t in tokens(title) if t not in STOPWORDS)
