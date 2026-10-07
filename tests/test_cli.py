from __future__ import annotations

import csv
import shutil

import pytest
from typer.testing import CliRunner

from jobhunt.cli import app
from jobhunt.tracker import Tracker


@pytest.fixture
def env(tmp_path, root, fixtures_dir, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copy(root / "profile.template.yaml", tmp_path / "profile.template.yaml")
    monkeypatch.setenv("JOBHUNT_TODAY", "2026-10-07")
    monkeypatch.setenv("JOBHUNT_PROFILE", str(root / "profile.example.yaml"))
    monkeypatch.setenv("JOBHUNT_COMPANIES", str(fixtures_dir / "companies.sample.yaml"))
    monkeypatch.setenv("COLUMNS", "250")
    return tmp_path


def run(*args, **kwargs):
    result = CliRunner().invoke(app, list(args), catch_exceptions=False, **kwargs)
    return result


def discover_and_score(fixtures_dir):
    assert run("discover", "--fixtures", str(fixtures_dir)).exit_code == 0
    assert run("score").exit_code == 0


def test_init_creates_profile(env):
    result = run("--profile", "profile.yaml", "init")
    assert result.exit_code == 0
    assert (env / "profile.yaml").read_text().startswith("# ====")
    assert (env / "data" / "jobhunt.db").exists()
    again = run("--profile", "profile.yaml", "init")
    assert "already exists" in again.output


def test_discover_reports_counts_and_errors(env, fixtures_dir):
    result = run("discover", "--fixtures", str(fixtures_dir))
    assert result.exit_code == 0
    assert "Northwind Quant: 5 postings" in result.output
    assert "Ghost Corp" in result.output  # missing board reported, run continues
    assert "11 new" in result.output
    rerun = run("discover", "--fixtures", str(fixtures_dir))
    assert "0 new, 11 refreshed" in rerun.output


def test_discover_company_filter(env, fixtures_dir):
    result = run("discover", "--fixtures", str(fixtures_dir), "-c", "Paylane")
    assert "3 new" in result.output
    assert run("discover", "-c", "nope").exit_code == 1


def test_score_lists_best_matches(env, fixtures_dir):
    run("discover", "--fixtures", str(fixtures_dir))
    result = run("score")
    assert result.exit_code == 0
    assert "Scored 11 job(s)" in result.output
    assert "Quantitative Research Intern - Summer 2027" in result.output
    assert "jobhunt prepare 1" in result.output
    assert "Scored 0 job(s)" in run("score").output
    assert "Scored 11 job(s)" in run("score", "--rescore").output


def test_prepare_status_followups_export(env, fixtures_dir):
    discover_and_score(fixtures_dir)
    result = run("prepare", "1")
    assert result.exit_code == 0, result.output
    assert "[en]" in result.output
    folder = next((env / "output").glob("0001-*"))
    assert (folder / "cv_en.pdf").exists()
    assert (folder / "cover_letter_en.txt").exists()
    assert (folder / "linkedin_note_en.txt").exists()

    assert "prepared" in run("status", "1").output
    result = run("status", "1", "aplicado", "--note", "careers site")
    assert "applied" in result.output and "2026-10-14" in result.output

    assert "No follow-ups due" in run("followups").output
    assert "Northwind" in run("followups", "--days-ahead", "7").output

    out = env / "apps.csv"
    assert run("export", str(out)).exit_code == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 11
    assert rows[0]["status"] == "applied"


def test_prepare_top_and_spanish(env, fixtures_dir):
    discover_and_score(fixtures_dir)
    result = run("prepare", "--top", "2")
    assert result.output.count("✓") == 2
    with Tracker(env / "data" / "jobhunt.db") as tracker:
        spanish_id = next(r.id for r in tracker.jobs() if r.job.title.startswith("Prácticas"))
    result = run("prepare", str(spanish_id))
    assert "[es]" in result.output


def test_prepare_requires_complete_profile(env, fixtures_dir, root):
    discover_and_score(fixtures_dir)
    result = run("--profile", str(root / "profile.template.yaml"), "prepare", "1")
    assert result.exit_code == 1
    assert "TODO" in result.output or "TODO" in (result.stderr or "")


def test_status_errors(env, fixtures_dir):
    discover_and_score(fixtures_dir)
    assert run("status", "1", "hired").exit_code == 1
    assert run("status", "999").exit_code == 1
    assert run("prepare").exit_code == 1


def test_add_manual_offer(env, tmp_path):
    desc = tmp_path / "desc.txt"
    desc.write_text("Summer Analyst 2027 programme in London. Excel and financial modelling.")
    args = [
        "add", "--company", "Example Bank", "--title", "Summer Analyst 2027 - IBD",
        "--url", "https://careers.example-bank.test/123", "--sector", "investment_banking",
        "--location", "London, United Kingdom", "--workplace", "onsite",
        "--description-file", str(desc),
    ]  # fmt: skip
    result = run(*args)
    assert result.exit_code == 0 and "Added job #1" in result.output
    assert "Already tracked" in run(*args).output
    assert (
        run("add", "--company", "X", "--title", "Y", "--url", "u", "--sector", "crypto").exit_code
        == 1
    )
    assert "Summer Analyst" in run("score").output
