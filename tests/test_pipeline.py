from __future__ import annotations

import copy
import datetime as dt

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
