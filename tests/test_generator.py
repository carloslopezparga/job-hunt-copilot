from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from jobhunt.generator import (
    IncompleteProfileError,
    choose_language,
    letters,
    prepare_application,
    slugify,
    tailoring,
)
from jobhunt.generator.cv_pdf import highlight
from jobhunt.generator.letters import (
    LETTER_MAX_WORDS,
    LETTER_MIN_WORDS,
    NOTE_MAX_CHARS,
    render_letter,
    render_linkedin_note,
)
from jobhunt.generator.llm import AnthropicPolisher, NullPolisher, fact_guard, get_polisher
from jobhunt.generator.tailoring import format_month, lower_first, tailor
from jobhunt.profile import load_profile
from jobhunt.scoring import rank
from jobhunt.sources import FixtureFetcher, discover, load_companies

TODAY = dt.date(2026, 10, 7)
TEMPLATES = Path(__file__).resolve().parent.parent / "src/jobhunt/generator/templates"


@pytest.fixture
def ranked(profile, fixtures_dir):
    companies = load_companies(fixtures_dir / "companies.sample.yaml")
    jobs = discover(companies, FixtureFetcher(fixtures_dir)).jobs
    return rank(jobs, profile, TODAY)


@pytest.fixture
def quant_job(ranked):
    return next(j for j, _ in ranked if j.title.startswith("Quantitative Research Intern"))


@pytest.fixture
def ds_job(ranked):
    return next(j for j, _ in ranked if j.title == "Data Science Intern (Remote)")


@pytest.fixture
def spanish_job(ranked):
    return next(j for j, _ in ranked if j.title.startswith("Prácticas"))


def allowed_sources(root: Path, job) -> str:
    """Everything a letter may legitimately draw from: profile, job, fixed boilerplate."""
    templates = "\n".join(p.read_text(encoding="utf-8") for p in TEMPLATES.glob("*.j2"))
    templates += f"\n{letters._GREETING} {letters._SIGNOFF} {tailoring._MONTHS_LONG}"
    profile_text = (root / "profile.example.yaml").read_text(encoding="utf-8")
    return f"{profile_text}\n{job.title}\n{job.company}\n{job.description}\n{templates}"


# ------------------------------------------------------------------ tailoring


def test_tailor_ranks_relevant_content(profile, quant_job):
    tp = tailor(profile, quant_job, "en", today=TODAY)
    assert "Python" in tp.keywords and "C++" in tp.keywords
    assert tp.projects[0].title == "Momentum backtester"
    assert tp.skills_highlighted[:2] == ["Python", "C++"]
    assert not set(tp.skills_highlighted) & set(tp.skills_other)
    assert tp.summary.startswith("Third-year Industrial Engineering student")  # sector summary
    assert tp.window and tp.window.label.startswith("Summer")
    assert tp.top_experience.organization.startswith("UPM Motorsport")


def test_tailor_only_uses_profile_bullets(profile, quant_job):
    tp = tailor(profile, quant_job, "en", today=TODAY)
    profile_bullets = {
        b["en"] if isinstance(b, dict) else b
        for item in profile.raw["experience"] + profile.raw["projects"]
        for b in item.get("bullets", [])
    }
    for item in tp.experience + tp.projects:
        assert set(item.bullets) <= profile_bullets


def test_tailor_reports_missing_translations(profile, ds_job):
    tp = tailor(profile, ds_job, "es", today=TODAY)
    assert any("motivations.fintech" in w for w in tp.warnings)
    assert tp.motivation_translated is False


# -------------------------------------------------------------------- letters


@pytest.mark.parametrize("lang", ["en", "es"])
def test_letters_are_150_to_200_words_for_all_good_matches(profile, ranked, lang):
    for job, score in ranked:
        if score.score < 65:
            continue
        letter = render_letter(tailor(profile, job, lang, today=TODAY))
        assert LETTER_MIN_WORDS <= letter.words <= LETTER_MAX_WORDS, (job.title, letter.words)
        assert letter.warnings == []
        assert "TODO" not in letter.text and "{{" not in letter.text


@pytest.mark.parametrize("lang", ["en", "es"])
def test_template_letter_invents_nothing(profile, quant_job, root, lang):
    """The no-LLM letter must pass the same fact guard applied to LLM output."""
    letter = render_letter(tailor(profile, quant_job, lang, today=TODAY))
    guard = fact_guard("", letter.text, allowed_sources(root, quant_job))
    assert guard.ok, guard.problems


def test_english_letter_content(profile, quant_job):
    text = render_letter(tailor(profile, quant_job, "en", today=TODAY)).text
    assert text.startswith("Dear Northwind Quant Hiring Team,")
    assert "Quantitative Research Intern - Summer 2027" in text
    assert "As Data & Simulation Engineer at UPM Motorsport" in text
    assert "I built a Python lap-time simulator" in text
    assert "June 2027 to December 2027" in text
    assert text.rstrip().endswith("Alex Example Martín")


def test_spanish_letter_never_mixes_languages(profile, ds_job):
    text = render_letter(tailor(profile, ds_job, "es", today=TODAY)).text
    assert text.startswith("Estimado equipo de selección de Paylane:")
    assert "I am drawn to products" not in text  # untranslated motivation dropped
    assert "inglés (C1, Cambridge C1 Advanced (2022))" in text or "Hablo" not in text
    assert "en remoto y compatible con mis estudios" in text


def test_thin_profile_warns_instead_of_padding(profile, quant_job):
    thin = copy.deepcopy(profile)
    thin.experience = []
    thin.projects = []
    thin.achievements = []
    thin.motivations = {}
    letter = render_letter(tailor(thin, quant_job, "en", today=TODAY))
    assert letter.words < LETTER_MIN_WORDS
    assert "not contain enough relevant material" in letter.warnings[0]


@pytest.mark.parametrize("lang", ["en", "es"])
def test_linkedin_note_fits_300_chars(profile, ranked, lang):
    for job, _ in ranked:
        note, warnings = render_linkedin_note(tailor(profile, job, lang, today=TODAY))
        assert len(note) <= NOTE_MAX_CHARS, note
        assert warnings == []
        assert ("[Name]" if lang == "en" else "[Nombre]") in note
        assert job.company in note


# ------------------------------------------------------------------------ PDF


def test_highlight_escapes_and_bolds():
    assert highlight("Used C++ & Python <fast>", ["Python", "C++"]) == (
        "Used <b>C++</b> &amp; <b>Python</b> &lt;fast&gt;"
    )
    assert highlight("Pythonic code", ["Python"]) == "Pythonic code"


def test_prepare_application_writes_reviewable_files(profile, quant_job, tmp_path):
    result = prepare_application(
        quant_job, profile, tmp_path, record_id=7, score=99, explanation="great", today=TODAY
    )
    assert result.directory.name == "0007-northwind-quant-quantitative-research-intern-summer-2027"
    assert result.lang == "en"
    for key in ("cv", "cover_letter", "linkedin_note", "job"):
        assert result.files[key].exists(), key
    reader = PdfReader(result.files["cv"])
    assert len(reader.pages) == 1
    pdf_text = reader.pages[0].extract_text()
    assert "Alex Example Martín" in pdf_text
    assert "Momentum backtester" in pdf_text
    assert "Cambridge C1 Advanced" in pdf_text
    job_md = result.files["job"].read_text(encoding="utf-8")
    assert quant_job.url in job_md
    assert "jobhunt status 7 applied" in job_md
    assert "warnings" not in result.files


def test_prepare_spanish_posting_defaults_to_spanish(profile, spanish_job, tmp_path):
    result = prepare_application(spanish_job, profile, tmp_path, today=TODAY)
    assert result.lang == "es"
    assert result.files["cv"].name == "cv_es.pdf"
    text = PdfReader(result.files["cv"]).pages[0].extract_text()
    assert "FORMACIÓN" in text and "EXPERIENCIA" in text
    assert "warnings" in result.files  # missing translations are reported


def test_prepare_refuses_profile_with_placeholders(root, quant_job, tmp_path):
    template = load_profile(root / "profile.template.yaml")
    with pytest.raises(IncompleteProfileError, match="TODO"):
        prepare_application(quant_job, template, tmp_path)


def test_choose_language(profile, quant_job, spanish_job):
    assert choose_language(quant_job, profile) == "en"
    assert choose_language(spanish_job, profile) == "es"
    assert choose_language(spanish_job, profile, "en") == "en"


# ------------------------------------------------------------------------ LLM


class FakePolisher:
    name = "fake"

    def __init__(self, transform):
        self.transform = transform

    def polish(self, text, *, kind, lang, facts, limits):
        return self.transform(text)


def test_fact_guard_detects_inventions():
    original = "As Data Engineer at UPM Motorsport, I built a simulator."
    sources = "Python pandas"
    assert fact_guard(original, original.replace("built", "created"), sources).ok
    bad = fact_guard(original, original + " It improved speed by 40%.", sources)
    assert not bad.ok and "40" in bad.problems[0]
    bad = fact_guard(
        original, original.replace("a simulator", "a simulator at Goldman Sachs"), sources
    )
    assert not bad.ok and "goldman" in bad.problems[0]


def test_rejected_polish_keeps_template(profile, quant_job, tmp_path):
    baseline = prepare_application(quant_job, profile, tmp_path / "a", today=TODAY)
    liar = FakePolisher(lambda t: t.replace("I built", "I single-handedly built at Google"))
    result = prepare_application(quant_job, profile, tmp_path / "b", polisher=liar, today=TODAY)
    assert any("fact guard" in w for w in result.warnings)
    assert result.files["cover_letter"].read_text() == baseline.files["cover_letter"].read_text()


def test_accepted_polish_is_used(profile, quant_job, tmp_path):
    honest = FakePolisher(lambda t: t.replace("I am writing to apply", "I would like to apply"))
    result = prepare_application(quant_job, profile, tmp_path, polisher=honest, today=TODAY)
    assert "I would like to apply" in result.files["cover_letter"].read_text()
    assert not any("fact guard" in w for w in result.warnings)


def test_failing_backend_falls_back(profile, quant_job, tmp_path):
    def boom(_):
        raise TimeoutError("network down")

    result = prepare_application(
        quant_job, profile, tmp_path, polisher=FakePolisher(boom), today=TODAY
    )
    assert any("failed" in w for w in result.warnings)
    assert result.files["cover_letter"].exists()


def test_anthropic_polisher_request_and_refusal():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        stop = "refusal" if "REFUSE" in kwargs["messages"][0]["content"] else "end_turn"
        return SimpleNamespace(
            stop_reason=stop, content=[SimpleNamespace(type="text", text="Polished.")]
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    polisher = AnthropicPolisher(model="claude-opus-5-5", client=client)
    assert polisher.polish("Draft.", kind="letter", lang="en", facts="f", limits="") == (
        "Polished."
    )
    assert calls[0]["model"] == "claude-opus-5-5"
    assert calls[0]["fallbacks"] == "default"
    assert "<candidate_facts>" in calls[0]["messages"][0]["content"]
    assert polisher.polish("REFUSE", kind="letter", lang="en", facts="", limits="") == "REFUSE"


def test_get_polisher():
    assert isinstance(get_polisher(None), NullPolisher)
    assert isinstance(get_polisher("none"), NullPolisher)
    with pytest.raises(ValueError):
        get_polisher("gpt")


# ---------------------------------------------------------------------- utils


def test_small_helpers():
    assert slugify("Prácticas Strategy & Operations") == "practicas-strategy-operations"
    assert format_month("2024-10", "en") == "Oct 2024"
    assert format_month("2024-10", "es") == "oct 2024"
    assert format_month("present", "es") == "Actualidad"
    assert lower_first("Built a model") == "built a model"
    assert lower_first("SQL pipelines") == "SQL pipelines"
