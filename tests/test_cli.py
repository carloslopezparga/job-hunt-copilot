from __future__ import annotations

import csv
import json
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
    assert "jobhunt prepare " in result.output
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
    assert next(r for r in rows if r["id"] == "1")["status"] == "applied"


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


# ------------------------------------------------------------ score filters & rescoring


def test_score_filters(env, fixtures_dir):
    run("discover", "--fixtures", str(fixtures_dir))
    run("score")
    every = run("score").output
    assert "Quantitative Research Intern - Summer 2027" in every  # London, visa-capped
    no_visa = run("score", "--no-visa").output
    assert "Quantitative Research Intern - Summer 2027" not in no_visa
    assert "Prácticas Strategy & Operations" in no_visa
    madrid = run("score", "--location", "Madrid").output
    assert "Prácticas Strategy & Operations" in madrid
    assert "Data Science Intern (Remote)" not in madrid
    remote = run("score", "--remote-only").output
    assert "Data Science Intern (Remote)" in remote
    assert "Prácticas Strategy & Operations" not in remote
    assert "Showing 2 of" in run("score", "--limit", "2").output


def test_score_rescores_when_profile_changes(env, fixtures_dir, root):
    run("discover", "--fixtures", str(fixtures_dir))
    assert "Scored 11 job(s)" in run("score").output
    assert "Scored 0 job(s)" in run("score").output
    edited = env / "profile.yaml"
    text = (root / "profile.example.yaml").read_text(encoding="utf-8")
    edited.write_text(
        text.replace('work_authorization: ["EU"]', 'work_authorization: ["EU", "UK"]')
    )
    result = run("--profile", str(edited), "score")
    assert "changed: re-scored every job" in result.output
    assert "Scored 11 job(s)" in result.output
    assert "Scored 11 job(s)" in run("--profile", str(edited), "score", "--rescore").output


def test_locations_command(env, fixtures_dir):
    run("discover", "--fixtures", str(fixtures_dir))
    out = run("locations").output
    assert "London, United Kingdom" in out and "visa" in out
    assert "Madrid, Spain" in out and "preferred" in out
    only_visa = run("locations", "--tier", "visa").output
    assert "Madrid, Spain" not in only_visa


def test_check_boards_and_promote(env, fixtures_dir, tmp_path):
    candidates = tmp_path / "candidates.yaml"
    candidates.write_text(
        "companies:\n"
        "  - {name: Paylane, ats: lever, slug: paylane, sector: fintech, region: eu}\n"
        "  - {name: Empty Co, ats: ashby, slug: emptyco, sector: ai_tech}\n"
        "  - {name: Ghost Corp, ats: greenhouse, slug: ghostcorp, sector: consulting}\n"
    )
    target = tmp_path / "companies.yaml"
    target.write_text(
        "companies:\n  - {name: Helix AI, ats: ashby, slug: helixai, sector: ai_tech}\n"
    )
    result = run("check-boards", "--file", str(candidates), "--fixtures", str(fixtures_dir),
                 "--promote", str(target))  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "1 pass" in result.output
    assert "not_found" in result.output and "empty" in result.output
    assert "Added 1 company" in result.output and "discarded 2" in result.output
    assert "slug: paylane" in target.read_text()
    assert "emptyco" not in target.read_text()


# --------------------------------------------------------------------------------- run


def add_posting(fixtures_copy, title, location):
    """Simulate a new posting appearing on Paylane's board between two runs."""
    path = fixtures_copy / "lever_paylane.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    new = dict(data[0])
    new.update(id=f"new-{len(data)}", text=title, workplaceType="onsite",
               hostedUrl=f"https://jobs.eu.lever.co/paylane/new-{len(data)}",
               categories={**data[0]["categories"], "location": location,
                           "allLocations": [location]})  # fmt: skip
    data.append(new)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def fixtures_copy(fixtures_dir, tmp_path):
    target = tmp_path / "fx"
    shutil.copytree(fixtures_dir, target)
    return target


def test_run_shows_only_new_offers(env, fixtures_copy):
    first = run("run", "--fixtures", str(fixtures_copy))
    assert first.exit_code == 0, first.output
    assert "Data Science Intern (Remote)" in first.output
    assert "Quantitative Research Intern - Summer 2027" not in first.output  # UK visa
    assert "Senior Quantitative Developer" not in first.output  # below threshold

    second = run("run", "--fixtures", str(fixtures_copy))
    assert "0 new offer(s)" in second.output

    add_posting(fixtures_copy, "Business Analyst Intern - Summer 2027", "Madrid, Spain")
    add_posting(fixtures_copy, "Data Analyst Intern - Summer 2027", "New York, NY")
    third = run("run", "--fixtures", str(fixtures_copy))
    assert "1 new offer(s)" in third.output
    assert "Business Analyst Intern - Summer 2027" in third.output
    assert "Data Analyst Intern - Summer 2027" not in third.output  # needs a US visa


def test_run_ignores_jobs_tracked_before_first_run(env, fixtures_copy):
    run("discover", "--fixtures", str(fixtures_copy))
    with Tracker(env / "data" / "jobhunt.db") as tracker:  # simulate a pre-v2 database
        tracker.conn.execute("UPDATE jobs SET first_seen_at = NULL")
        tracker.conn.commit()
    result = run("run", "--fixtures", str(fixtures_copy))
    assert "First run" in result.output and "0 new offer(s)" in result.output


def test_run_export_and_prepare(env, fixtures_copy):
    result = run("run", "--fixtures", str(fixtures_copy), "--prepare",
                 "--export", "shortlist.md")  # fmt: skip
    assert result.exit_code == 0, result.output
    md = (env / "shortlist.md").read_text(encoding="utf-8")
    assert "# Job shortlist" in md and "Data Science Intern (Remote)" in md
    assert "https://jobs.eu.lever.co/paylane/" in md and "**Why:**" in md
    assert "**Prepared material:**" in md
    assert list((env / "output").glob("*/cv_*.pdf"))


def test_run_prepare_refuses_todo_profile(env, fixtures_copy, root):
    result = run("--profile", str(root / "profile.template.yaml"), "run",
                 "--fixtures", str(fixtures_copy), "--prepare")  # fmt: skip
    assert result.exit_code == 0
    assert "TODO field(s)" in result.output and "nothing was generated" in result.output
    assert not (env / "output").exists()


def test_run_notify_without_credentials_still_works(env, fixtures_copy, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = run("run", "--fixtures", str(fixtures_copy), "--notify", "telegram")
    assert result.exit_code == 0
    assert "no notification sent" in result.output


def test_run_notify_telegram_with_fake_http(env, fixtures_copy, monkeypatch):
    calls = []

    def fake_post(self, url, body, headers):
        calls.append((url, body))
        return {"ok": True}

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr("jobhunt.notify.UrllibPoster.post", fake_post)
    monkeypatch.setattr("jobhunt.notify.TelegramNotifier.delay", 0)
    result = run("run", "--fixtures", str(fixtures_copy), "--notify", "telegram", "--prepare")
    assert result.exit_code == 0, result.output
    messages = [b for u, b in calls if u.endswith("/sendMessage")]
    documents = [b for u, b in calls if u.endswith("/sendDocument")]
    assert messages and len(documents) == 2 * len(messages)  # CV + letter per offer
    assert all(u.startswith("https://api.telegram.org/bot999:fake/") for u, _ in calls)
    assert "Telegram: notified" in result.output
    again = run("run", "--fixtures", str(fixtures_copy), "--notify", "telegram")
    assert "Nothing new to notify" in again.output


def test_run_rejects_unknown_notifier(env, fixtures_copy):
    assert run("run", "--fixtures", str(fixtures_copy), "--notify", "email").exit_code == 1


# ------------------------------------------------------------------------------- bot


def test_bot_needs_telegram_credentials(env, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = run("bot")
    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in result.output and "TELEGRAM_CHAT_ID" in result.output


def test_bot_rejects_a_bad_daily_time(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    result = run("bot", "--daily-at", "25:99")
    assert result.exit_code == 1 and "HH:MM" in result.output


def test_bot_rejects_an_unauthorized_token(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(
        "jobhunt.notify.UrllibPoster.post",
        lambda self, url, body, headers: {"ok": False, "description": "Unauthorized"},
    )
    result = run("bot", "--no-daily")
    assert result.exit_code == 1 and "Unauthorized" in result.output
    assert "999:fake" not in result.output


def test_bot_answers_its_chat_and_stops_on_ctrl_c(env, monkeypatch):
    sent = []
    polls = {"n": 0}

    def fake_post(self, url, body, headers):
        method = url.rsplit("/", 1)[1]
        payload = json.loads(body) if headers["Content-Type"] == "application/json" else {}
        if method == "getMe":
            return {"ok": True, "result": {"username": "my_bot"}}
        if method == "getUpdates":
            polls["n"] += 1
            if polls["n"] > 1:
                raise KeyboardInterrupt
            return {"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/ayuda"}},
                {"update_id": 2, "message": {"chat": {"id": 13}, "text": "/ayuda"}},
            ]}  # fmt: skip
        sent.append((method, payload.get("chat_id")))
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr("jobhunt.notify.UrllibPoster.post", fake_post)
    result = run("bot", "--no-daily")
    assert result.exit_code == 0, result.output
    assert "@my_bot" in result.output and "Bot stopped" in result.output
    # the start-up message and the /ayuda answer go to chat 42; the stranger gets nothing
    assert sent == [("sendMessage", "42"), ("sendMessage", "42")]
