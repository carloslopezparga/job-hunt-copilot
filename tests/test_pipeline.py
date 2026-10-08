from __future__ import annotations

import copy
import datetime as dt
import json

import yaml

from jobhunt.models import Job
from jobhunt.pipeline import FINGERPRINT_KEY, LocationFilter, apply_filter, score_tracked
from jobhunt.profile import parse_profile
from jobhunt.sources import FixtureFetcher, discover, load_companies
from jobhunt.tracker import Tracker

TODAY = dt.date(2026, 10, 7)


def load_fixture_jobs(fixtures_dir):
    companies = load_companies(fixtures_dir / "companies.sample.yaml")
    return discover(companies, FixtureFetcher(fixtures_dir)).jobs


def edited_profile(root, mutate):
    data = yaml.safe_load((root / "profile.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    mutate(data)
    return parse_profile(data)


def test_scores_only_new_jobs_until_profile_changes(tmp_path, root, profile, fixtures_dir):
    with Tracker(tmp_path / "t.db") as tracker:
        tracker.upsert_jobs(load_fixture_jobs(fixtures_dir)[:4])
        first = score_tracked(tracker, profile, TODAY)
        assert (first.scored, first.reason) == (4, "new jobs")
        assert tracker.get_meta(FINGERPRINT_KEY).startswith("v")

        assert score_tracked(tracker, profile, TODAY).scored == 0  # nothing new, same profile

        madrid_lover = edited_profile(
            root, lambda d: d["preferences"]["locations"].update(preferred=["London"])
        )
        changed = score_tracked(tracker, madrid_lover, TODAY)
        assert (changed.scored, changed.reason) == (4, "profile changed")
        assert score_tracked(tracker, madrid_lover, TODAY).scored == 0

        forced = score_tracked(tracker, madrid_lover, TODAY, force=True)
        assert (forced.scored, forced.reason) == (4, "forced")


def test_profile_change_updates_stored_scores(tmp_path, root, profile, fixtures_dir):
    with Tracker(tmp_path / "t.db") as tracker:
        tracker.upsert_jobs(load_fixture_jobs(fixtures_dir))
        score_tracked(tracker, profile, TODAY)
        london = next(r for r in tracker.jobs() if r.job.title.startswith("Quantitative Res"))
        assert london.score <= 30
        uk_ok = edited_profile(
            root, lambda d: d["personal"].update(work_authorization=["EU", "UK"])
        )
        score_tracked(tracker, uk_ok, TODAY)
        assert tracker.get(london.id).score > 30


def test_fingerprint_ignores_comments_and_key_order(root):
    a = edited_profile(root, lambda d: None)
    b = edited_profile(root, lambda d: d.update(dict(reversed(list(d.items())))))
    assert a.fingerprint() == b.fingerprint()
    c = edited_profile(root, lambda d: d["skills"]["technical"].append("Rust"))
    assert c.fingerprint() != a.fingerprint()


def job(location, workplace="unknown"):
    return Job("lever", location, "Co", "co", "Data Intern", "u", location=location,
               workplace_type=workplace)  # fmt: skip


def test_location_filter(profile):
    jobs = [job("Madrid, Spain"), job("Remote - EU"), job("London, UK"), job("Zurich")]
    names = lambda flt: [j.location for j in jobs if flt.accepts(j, profile)]  # noqa: E731
    assert names(LocationFilter()) == ["Madrid, Spain", "Remote - EU", "London, UK", "Zurich"]
    assert names(LocationFilter(no_visa=True)) == ["Madrid, Spain", "Remote - EU", "Zurich"]
    assert names(LocationFilter(remote_only=True)) == ["Remote - EU"]
    assert names(LocationFilter(text="Spain")) == ["Madrid, Spain"]
    assert names(LocationFilter(text="Remote - EU")) == ["Remote - EU"]
    assert names(LocationFilter(tiers=["preferred", "acceptable"])) == [
        "Madrid, Spain",
        "Remote - EU",
    ]


def test_location_filter_from_profile(profile):
    flt = LocationFilter.from_profile(profile)
    assert flt.no_visa and not flt.remote_only
    assert flt.tiers == ["preferred", "acceptable", "other"]


def test_apply_filter_on_records(tmp_path, profile):
    with Tracker(tmp_path / "t.db") as tracker:
        tracker.upsert_jobs([job("Madrid"), job("New York, NY")])
        kept = apply_filter(tracker.jobs(), profile, LocationFilter(no_visa=True))
        assert [r.job.location for r in kept] == ["Madrid"]


def _legacy_database(path, jobs):
    """A tracker file as written by v1: no first_seen_at column, no scoring fingerprint,
    scores computed by older rules."""
    import sqlite3

    from jobhunt.tracker import _SCHEMA

    jobs_table = _SCHEMA.split("CREATE TABLE IF NOT EXISTS meta")[0]  # v1 had no meta table
    jobs_table = "\n".join(x for x in jobs_table.splitlines() if "first_seen_at" not in x)
    with sqlite3.connect(path) as conn:
        conn.executescript(jobs_table)
        for i, job in enumerate(jobs, start=1):
            conn.execute(
                "INSERT INTO jobs (id, uid, dedup_key, source, external_id, company, company_slug,"
                " sector, title, url, location, locations, workplace_type, employment_type,"
                " department, description, first_seen, last_seen, score, score_explanation,"
                " status, status_changed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, job.uid, f"k{i}", job.source, job.external_id, job.company, job.company_slug,
                 job.sector, job.title, job.url, job.location, json.dumps(job.locations),
                 job.workplace_type, job.employment_type, job.department, job.description,
                 "2026-09-01", "2026-09-01", 99, "old rules", "new", "2026-09-01"),
            )  # fmt: skip


def test_migrated_database_is_rescored_automatically(tmp_path, profile, fixtures_dir):
    db = tmp_path / "old.db"
    _legacy_database(db, load_fixture_jobs(fixtures_dir)[:4])
    with Tracker(db) as tracker:  # opening migrates the schema
        assert tracker.get_meta(FINGERPRINT_KEY) is None
        assert {r.score for r in tracker.jobs()} == {99}

        run = score_tracked(tracker, profile, TODAY)  # no --rescore / force
        assert (run.scored, run.reason) == (4, "migrated database") and run.rescored_all
        assert 99 not in {r.score for r in tracker.jobs()}
        assert score_tracked(tracker, profile, TODAY).reason == "new jobs"  # and only once


def test_empty_database_without_fingerprint_is_not_a_migration(tmp_path, profile, fixtures_dir):
    with Tracker(tmp_path / "t.db") as tracker:
        tracker.upsert_jobs(load_fixture_jobs(fixtures_dir)[:2])
        assert score_tracked(tracker, profile, TODAY).reason == "new jobs"
