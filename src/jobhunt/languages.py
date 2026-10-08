"""Spot human-language requirements in postings ("Italian Fluent", "Portuguese speaking",
"French Market") and compare them with the languages in the profile.

A language is only a *requirement* when the text asks for it with a proficiency or market
phrase and the sentence is not softened ("a plus", "nice to have", "valorable"...).
"""

from __future__ import annotations

import re

from jobhunt.profile import Profile
from jobhunt.textutils import fold

# canonical name -> spellings (accent-folded) in English and Spanish
LANGUAGE_NAMES: dict[str, list[str]] = {
    "English": ["english", "ingles"],
    "Spanish": ["spanish", "espanol", "castellano"],
    "French": ["french", "frances"],
    "German": ["german", "aleman"],
    "Italian": ["italian", "italiano"],
    "Portuguese": ["portuguese", "portugues"],
    "Dutch": ["dutch", "neerlandes", "holandes"],
    "Catalan": ["catalan"],
    "Polish": ["polish", "polaco"],
    "Swedish": ["swedish", "sueco"],
    "Danish": ["danish", "danes"],
    "Norwegian": ["norwegian", "noruego"],
    "Finnish": ["finnish", "finlandes"],
    "Russian": ["russian", "ruso"],
    "Czech": ["czech", "checo"],
    "Greek": ["greek", "griego"],
    "Romanian": ["romanian", "rumano"],
    "Turkish": ["turkish", "turco"],
    "Arabic": ["arabic", "arabe"],
    "Chinese": ["chinese", "mandarin", "cantonese", "chino"],
    "Japanese": ["japanese", "japones"],
    "Korean": ["korean", "coreano"],
    "Hindi": ["hindi"],
}
_CANONICAL = {alias: name for name, aliases in LANGUAGE_NAMES.items() for alias in aliases}
_LANG = "(" + "|".join(sorted(map(re.escape, _CANONICAL), key=len, reverse=True)) + ")"

_LEVEL = (
    r"(?:fluent|fluency|native|mother tongue|bilingual|proficient|proficiency|"
    r"speaking|speaker|speakers|spoken|c1|c2|fluido|fluida|nativo|nativa|avanzado|avanzada|"
    r"bilingue|market|markets|language skills?|written and spoken|business level)"
)
_PATTERNS = [
    # "Italian fluent", "Portuguese speaking", "French Market", "French (native)"
    re.compile(rf"(?<![\w]){_LANG}\W+(?:\w+\W+){{0,1}}{_LEVEL}(?![\w])"),
    # "Italian-speaking", "French speaker"
    re.compile(rf"(?<![\w]){_LANG}[\s-]+(?:speaking|speaker|speakers|hablante)"),
]
# "fluent in Italian", "native Spanish", "bilingual French/German", "dominio del italiano":
# right after the trigger, filler words may link the languages named ("fluent in German and
# English"); any other word ends the list ("cloud native company ... French Quarter").
_TRIGGER = re.compile(
    r"(?<![\w])(?:fluent|fluency|native|proficient|proficiency|bilingual|mother tongue|"
    r"command|knowledge|fluidez|dominio|nivel \w+)(?![\w])(?=(.{0,60}))"  # lookahead: may overlap
)
_FILLER = {
    "in", "of", "en", "de", "del", "both", "level", "written", "spoken", "verbal", "and", "or",
    "y", "e", "business", "professional", "working", "strong", "good", "excellent", "el", "la",
    "al", "a", "the", "also",
}  # fmt: skip
_SOFTENERS = re.compile(
    r"\b(plus|bonus|nice to have|nice-to-have|advantage|desirable|preferred|valued|"
    r"an asset|ideally|ventaja|valorable|valorar|valorara|se valora|deseable|"
    r"optional|opcional|not required|no es necesario)\b"
)
_SENTENCES = re.compile(r"[.\n;•!?]+|\s-\s|\s\*\s")

_ORDER = ["a1", "a2", "b1", "b2", "c1", "c2", "native"]
MIN_LEVEL = "b2"  # below this a language does not cover a requirement


def required_languages(text: str) -> list[str]:
    """Canonical names of the languages the text requires (in order of appearance)."""
    found: dict[str, None] = {}
    for sentence in _SENTENCES.split(fold(text)):
        if not sentence.strip() or _SOFTENERS.search(sentence):
            continue
        for pattern in _PATTERNS:
            for m in pattern.finditer(sentence):
                found.setdefault(_CANONICAL[m.group(1)], None)
        for m in _TRIGGER.finditer(sentence):
            for word in re.findall(r"[a-z]+", m.group(1)):
                if word in _CANONICAL:
                    found.setdefault(_CANONICAL[word], None)
                elif word not in _FILLER:
                    break
    return list(found)


_WORD_LEVELS = {
    "native": "native", "nativo": "native", "mother tongue": "native", "bilingual": "native",
    "fluent": "c1", "advanced": "c1", "avanzado": "c1", "proficient": "c1", "upper": "b2",
    "intermediate": "b1", "intermedio": "b1", "basic": "a2", "basico": "a2",
}  # fmt: skip


def _level_rank(level: str) -> int:
    key = fold(level).strip()
    if "todo" in key:  # not filled in yet: do not penalise the offer for it
        return len(_ORDER)
    for word, mapped in _WORD_LEVELS.items():
        if key.startswith(word):
            return _ORDER.index(mapped)
    for i, name in enumerate(_ORDER):
        if key.startswith(name):
            return i
    return -1


def spoken_languages(profile: Profile) -> set[str]:
    """Canonical names of the languages the profile covers at B2 or better."""
    spoken = set()
    for item in profile.languages:
        name = _CANONICAL.get(fold(item.language).strip())
        if name and _level_rank(item.level) >= _ORDER.index(MIN_LEVEL):
            spoken.add(name)
    return spoken


def missing_languages(title: str, description: str, profile: Profile) -> list[str]:
    """Languages the posting requires that the profile does not cover."""
    have = spoken_languages(profile)
    return [lang for lang in required_languages(f"{title}\n{description}") if lang not in have]
