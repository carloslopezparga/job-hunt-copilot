"""Small, dependency-free text helpers: HTML to text, keyword matching, heuristics."""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None, *, escaped: bool = False) -> str:
    """Convert (optionally HTML-escaped) HTML to readable plain text."""
    if not raw:
        return ""
    if escaped:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fold(text: str) -> str:
    """Lowercase and strip accents so 'Prácticas' matches 'practicas'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def contains_term(text: str, term: str) -> bool:
    """Whole-word, accent- and case-insensitive match (handles 'C++', 'A/B', 'M&A')."""
    term_f = fold(term).strip()
    if not term_f:
        return False
    pattern = r"(?<![\w])" + re.escape(term_f) + r"(?![\w])"
    return re.search(pattern, fold(text)) is not None


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Return the subset of ``terms`` present in ``text`` (original spelling, in order)."""
    return [t for t in terms if contains_term(text, t)]


def normalize_key(text: str) -> str:
    """Aggressive normalization for duplicate detection."""
    text = fold(text)
    text = re.sub(r"[\(\)\[\],/|\-–—:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


INTERNSHIP_TERMS = [
    "intern",
    "interns",
    "internship",
    "internships",
    "summer analyst",
    "summer associate",
    "off-cycle",
    "off cycle",
    "industrial placement",
    "placement year",
    "working student",
    "werkstudent",
    "praktikum",
    "stagiaire",
    "practicas",
    "becario",
    "becaria",
    "trainee",
]

REMOTE_TERMS = ["remote", "remoto", "teletrabajo", "work from home", "fully distributed"]


def looks_like_internship(*texts: str) -> bool:
    return any(contains_term(t, term) for t in texts if t for term in INTERNSHIP_TERMS)


def looks_remote(*texts: str) -> bool:
    return any(contains_term(t, term) for t in texts if t for term in REMOTE_TERMS)


_SPANISH_HINTS = {"de", "la", "el", "los", "las", "y", "en", "para", "con", "por", "una", "que"}
_ENGLISH_HINTS = {"the", "and", "of", "to", "in", "for", "with", "you", "we", "our", "a", "is"}


def guess_language(text: str) -> str:
    """Very small stopword-based detector: returns 'es' or 'en'."""
    words = re.findall(r"[a-záéíóúñ]+", (text or "").lower())
    es = sum(w in _SPANISH_HINTS for w in words)
    en = sum(w in _ENGLISH_HINTS for w in words)
    return "es" if es > en else "en"


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))
