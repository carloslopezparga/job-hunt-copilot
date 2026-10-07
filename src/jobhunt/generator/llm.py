"""Optional LLM polishing behind a small interface.

The template output is always produced first and is complete on its own. A
``Polisher`` may only *rephrase* it. Every polished text passes ``fact_guard``:
if it introduces numbers, capitalised names or URLs that are not in the
original text, the profile or the job posting, or if it breaks the length
limits, the polished version is discarded and the template version is kept.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You edit job-application texts for a university student.
Rules, all mandatory:
- Improve flow and concision only. Keep the same language as the input.
- Do NOT add any fact, number, skill, tool, employer, project, achievement, date or claim
  that is not already in the INPUT TEXT or the CANDIDATE FACTS. Do not exaggerate
  (no "expert", "extensive", "led" unless already stated).
- Keep every concrete fact that is in the input.
- Return only the rewritten text, without preamble, quotes or markdown."""


class Polisher(Protocol):
    name: str

    def polish(self, text: str, *, kind: str, lang: str, facts: str, limits: str) -> str:
        """Return a rephrased version of ``text`` (or ``text`` unchanged)."""


class NullPolisher:
    """No-LLM mode: returns the template text untouched."""

    name = "none"

    def polish(self, text: str, *, kind: str, lang: str, facts: str, limits: str) -> str:
        return text


class AnthropicPolisher:
    """Rephrases text with Claude via the official ``anthropic`` SDK (optional extra).

    Install with ``pip install 'job-hunt-copilot[llm]'`` and set ``ANTHROPIC_API_KEY``.
    The model can be overridden with ``JOBHUNT_LLM_MODEL``.
    """

    name = "anthropic"

    def __init__(self, model: str | None = None, client=None) -> None:  # noqa: ANN001
        self.model = model or os.environ.get("JOBHUNT_LLM_MODEL", DEFAULT_MODEL)
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise RuntimeError(
                    "the 'anthropic' package is not installed: pip install 'job-hunt-copilot[llm]'"
                ) from exc
            client = anthropic.Anthropic()
        self.client = client

    def polish(self, text: str, *, kind: str, lang: str, facts: str, limits: str) -> str:
        prompt = (
            f"Rewrite this {kind} ({'Spanish' if lang == 'es' else 'English'}). {limits}\n\n"
            f"<candidate_facts>\n{facts}\n</candidate_facts>\n\n"
            f"<input_text>\n{text}\n</input_text>"
        )
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            # Server-side refusal fallback: a declined request is retried on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            return text
        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return "".join(parts).strip() or text


def get_polisher(name: str | None) -> Polisher:
    if not name or name == "none":
        return NullPolisher()
    if name == "anthropic":
        return AnthropicPolisher()
    raise ValueError(f"unknown LLM backend '{name}' (use 'none' or 'anthropic')")


# ------------------------------------------------------------------- fact guard

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://\S+|www\.\S+")
_CAP_WORD = re.compile(r"\b[A-ZÁÉÍÓÚÑ][\w&+#.-]*[\w+#]|\b[A-Z]\b")
_SENTENCE_START = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[«\"“(]\s*)$")


@dataclass
class GuardResult:
    ok: bool
    problems: list[str]


def _capitalised_terms(text: str) -> set[str]:
    """Capitalised words that are not merely sentence-initial."""
    terms = set()
    for m in _CAP_WORD.finditer(text):
        if _SENTENCE_START.search(text[: m.start()]):
            continue
        terms.add(m.group(0).strip(".").lower())
    return terms


def fact_guard(original: str, polished: str, sources: str) -> GuardResult:
    """Reject polished text that introduces facts absent from ``original`` + ``sources``."""
    allowed = f"{original}\n{sources}"
    allowed_lower = allowed.lower()
    problems: list[str] = []

    new_numbers = sorted(set(_NUMBER.findall(polished)) - set(_NUMBER.findall(allowed)))
    if new_numbers:
        problems.append(f"new numbers: {', '.join(new_numbers)}")
    new_urls = sorted(set(_URL.findall(polished)) - set(_URL.findall(allowed)))
    if new_urls:
        problems.append(f"new links: {', '.join(new_urls)}")
    new_names = sorted(t for t in _capitalised_terms(polished) if t not in allowed_lower)
    if new_names:
        problems.append(f"new names/terms: {', '.join(new_names)}")
    return GuardResult(ok=not problems, problems=problems)
