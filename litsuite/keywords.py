"""Relevance of candidate papers, and keyword suggestions mined from your library.

Score of a candidate (all weights are in settings.yaml under ``scoring``)::

    + focus_title       for each focus keyword in the title      (default 3.0)
    + focus_snippet     ... found only in the snippet/abstract   (1.0)
    + secondary_title   for each secondary keyword in the title  (1.0)
    + secondary_snippet ... only in the snippet                  (0.5)
    + learned_title     for each learned keyword in the title    (0.5, at most learned_cap = 2.0)
    + similarity_weight x similarity to your existing papers     (4.0 x 0..1)
    - exclude_penalty   if any exclude keyword appears           (10.0)

Keywords match whole words, ignore case/plurals/hyphens, and understand your
abbreviations both ways ("DRL" == "deep reinforcement learning").
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import yaml

from litsuite.textutils import STOPWORDS, normalize, stem_tokens, tokens


def _variants(text: str, abbreviator=None, isolate: bool = False) -> list:
    out = [text]
    if abbreviator:
        out += [abbreviator.expand(text, isolate=isolate), abbreviator.abbreviate(text)]
    return out


def _contains(hay: list, needle: list) -> bool:
    n = len(needle)
    if not n or n > len(hay):
        return False
    first = needle[0]
    for i, tok in enumerate(hay[: len(hay) - n + 1]):
        if tok == first and hay[i:i + n] == needle:
            return True
    return False


class KeywordMatcher:
    def __init__(self, keyword: str, abbreviator=None):
        self.keyword = keyword
        forms = set()
        for v in _variants(keyword, abbreviator):
            st = stem_tokens(v)
            if st:
                forms.add(tuple(st))
                if len(st) > 1:
                    forms.add(("".join(st),))   # "multi agent" also matches "multiagent"
        self.forms = [list(f) for f in forms]

    def found_in(self, text_forms: list) -> bool:
        return any(_contains(tf, f) for tf in text_forms for f in self.forms)


class LibraryIndex:
    """TF-IDF vectors of the papers you already have (title + abstract)."""

    def __init__(self, docs: list):
        self.df: Counter = Counter()
        vectors = []
        for d in docs:
            toks = [t for t in stem_tokens(d) if t not in STOPWORDS and len(t) > 2]
            tf = Counter(toks)
            vectors.append(tf)
            self.df.update(tf.keys())
        self.n = max(len(docs), 1)
        self.vectors = [self._weigh(tf) for tf in vectors if tf]

    def _idf(self, term: str) -> float:
        return math.log((1 + self.n) / (1 + self.df.get(term, 0))) + 1

    def _weigh(self, tf: Counter) -> dict:
        vec = {t: (1 + math.log(c)) * self._idf(t) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def similarity(self, text: str, top_k: int = 3) -> float:
        toks = [t for t in stem_tokens(text) if t not in STOPWORDS and len(t) > 2]
        if not toks or not self.vectors:
            return 0.0
        q = self._weigh(Counter(toks))
        sims = sorted((sum(w * v.get(t, 0.0) for t, w in q.items()) for v in self.vectors), reverse=True)
        best = sims[:top_k]
        return sum(best) / len(best)


class RelevanceScorer:
    def __init__(self, keywords: dict, scoring: dict, abbreviator=None, learned=(), library_docs=()):
        self.w = scoring
        self.abbreviator = abbreviator
        kw = keywords or {}
        self.focus = [KeywordMatcher(k, abbreviator) for k in kw.get("focus") or []]
        self.secondary = [KeywordMatcher(k, abbreviator) for k in kw.get("secondary") or []]
        self.exclude = [KeywordMatcher(k, abbreviator) for k in kw.get("exclude") or []]
        explicit = [m for group in (self.focus, self.secondary, self.exclude) for m in group]
        self.learned = [m for m in (KeywordMatcher(k, abbreviator)
                                    for k in (learned if kw.get("use_learned", True) else []))
                        if not any(e.found_in(m.forms) or m.found_in(e.forms) for e in explicit)]
        use_sim = kw.get("use_library_similarity", True) and library_docs
        self.index = LibraryIndex(list(library_docs)) if use_sim else None

    def _forms(self, text: str) -> list:
        return [stem_tokens(v) for v in _variants(text or "", self.abbreviator, isolate=True)]

    def score(self, title: str, snippet: str = "") -> tuple:
        """Return (score, reasons)."""
        w, reasons, score = self.w, [], 0.0
        tf, sf = self._forms(title), self._forms(snippet)
        focus_hit = False
        for m in self.focus:
            if m.found_in(tf):
                score += w["focus_title"]
                reasons.append(f"focus:{m.keyword}")
                focus_hit = True
            elif sf and m.found_in(sf):
                score += w["focus_snippet"]
                reasons.append(f"focus(snippet):{m.keyword}")
                focus_hit = True
        for m in self.secondary:
            if m.found_in(tf):
                score += w["secondary_title"]
                reasons.append(f"kw:{m.keyword}")
            elif sf and m.found_in(sf):
                score += w["secondary_snippet"]
                reasons.append(f"kw(snippet):{m.keyword}")
        learned_total = 0.0
        for m in self.learned:
            if learned_total >= w["learned_cap"]:
                break
            if m.found_in(tf):
                learned_total += w["learned_title"]
                reasons.append(f"learned:{m.keyword}")
        score += min(learned_total, w["learned_cap"])
        if self.index is not None:
            sim = self.index.similarity(f"{title} {snippet}")
            score += w["similarity_weight"] * sim
            reasons.append(f"similarity:{sim:.2f}")
        for m in self.exclude:
            if m.found_in(tf) or (sf and m.found_in(sf)):
                score -= w["exclude_penalty"]
                reasons.append(f"EXCLUDED:{m.keyword}")
        if w.get("require_focus") and self.focus and not focus_hit:
            score = min(score, 0.0)
            reasons.append("no focus keyword")
        return round(score, 2), reasons


# ---------------------------------------------------------------------------
# Keyword suggestions from the library
# ---------------------------------------------------------------------------
# Words that make poor phrase edges ("while latency", "high reliability"...).
_EDGE_WORDS = STOPWORDS | {
    "while", "high", "low", "real", "multiple", "diverse", "different", "various", "several", "many", "large",
    "key", "main", "major", "efficient", "effective", "proposed", "existing", "recent", "future", "current",
    "significant", "significantly", "better", "best", "optimal", "efficiently", "effectively", "specific",
    "simulation", "results", "result", "problem", "solution", "solutions", "paper", "article", "times",
    "time", "level", "user", "users", "case", "cases", "end", "terms", "order", "number", "set", "way",
    "need", "needs", "make", "makes", "however", "thus", "therefore", "further", "finally", "addition",
}
_ACRONYM = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)?)(?![A-Za-z0-9])")


def _is_acronym(tok: str) -> bool:
    caps = sum(c.isupper() for c in tok)
    return 2 <= len(tok) <= 10 and caps >= 2 and caps >= len(tok.replace("-", "")) / 2


def suggest_keywords(rows, top: int = 40, min_docs: int = 3) -> list:
    """Most characteristic phrases (2-3 words) and acronyms of your library: [(phrase, n_papers)]."""
    df: Counter = Counter()
    surface: dict = {}
    for r in rows:
        if r.get("status") == "missing":
            continue
        title = r.get("title_official") or r.get("title_raw") or ""
        text = f"{title}. {r.get('abstract') or ''}"
        seen = set()
        for sentence in re.split(r"[.;:!?()\[\]]", text):
            words = tokens(sentence)
            for n in (2, 3):
                for i in range(len(words) - n + 1):
                    gram = words[i:i + n]
                    if gram[0] in _EDGE_WORDS or gram[-1] in _EDGE_WORDS or any(w.isdigit() for w in gram):
                        continue
                    phrase = " ".join(gram)
                    key = " ".join(stem_tokens(phrase))
                    seen.add(key)
                    surface.setdefault(key, Counter())[phrase] += 1
        for tok in _ACRONYM.findall(text):
            if _is_acronym(tok) and tok.lower() not in STOPWORDS:
                key = "acr:" + tok
                seen.add(key)
                surface.setdefault(key, Counter())[tok] += 1
        df.update(seen)
    cands = [(k, c) for k, c in df.items() if c >= min_docs]
    # Prefer longer phrases: drop a phrase when a longer one containing it is almost as frequent.
    by_len = sorted(cands, key=lambda kc: -len(kc[0].split()))
    kept: dict = {}
    for k, c in by_len:
        if any(f" {k} " in f" {longer} " and lc >= 0.8 * c for longer, lc in kept.items()):
            continue
        kept[k] = c
    ranked = sorted(kept.items(), key=lambda kc: -(kc[1] * (1 + 0.5 * (len(kc[0].split()) - 1))))
    return [(surface[k].most_common(1)[0][0], c) for k, c in ranked[:top]]


def write_learned_keywords(suggestions: list, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Keywords learned from this project's catalog (litsuite keywords suggest --save).\n"
              "# Used with a small weight when 'keywords.use_learned' is true. Delete lines you do not want;\n"
              "# promote important ones to 'focus' or 'secondary' in settings.yaml.\n"
              "# Format:  - phrase      # number of papers containing it\n")
    lines = [f"- {yaml.safe_dump(k, allow_unicode=True).strip().splitlines()[0]:<45} # {c}" for k, c in suggestions]
    path.write_text(header + "keywords:\n" + "\n".join("  " + l for l in lines) + "\n", encoding="utf-8")
    return path


def load_learned_keywords(path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("keywords") if isinstance(data, dict) else data
    return [str(k) for k in (items or []) if k]


def library_documents(rows) -> list:
    return [f"{r.get('title_official') or r.get('title_raw') or ''} {(r.get('abstract') or '')[:1500]}"
            for r in rows if r.get("status") != "missing"]


def build_scorer(project, rows) -> RelevanceScorer:
    learned = load_learned_keywords(project.learned_keywords_path)
    return RelevanceScorer(project.keywords, project.scoring, project.abbreviator, learned,
                           library_documents(rows))


__all__ = ["RelevanceScorer", "KeywordMatcher", "LibraryIndex", "suggest_keywords", "build_scorer",
           "load_learned_keywords", "write_learned_keywords", "library_documents", "normalize"]
