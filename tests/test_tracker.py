from __future__ import annotations

import csv
import datetime as dt

import pytest

from jobhunt.models import Job
from jobhunt.tracker import FOLLOWUP_DAYS, Tracker, TrackerError, normalize_status

DAY0 = dt.date(2026, 10, 7)


class Clock:
    def __init__(self, day: dt.date) -> None:
        self.day = day

    def __call__(self) -> dt.date:
        return self.day


def job(n: int, **kw) -> Job:
    base = dict(
        source="lever",
        external_id=str(n),
        company="Paylane",
        company_slug="paylane",
        sector="fintech",
        title=f"Data Intern {n}",
        url=f"https://example.test/{n}",
        location="Remote - Europe",
        workplace_type="remote",
        employment_type="internship",
        description="desc",
        posted_at="2026-10-01",
    )
    base.update(kw)
    return Job(**base)


@pytest.fixture
def clock():
    return Clock(DAY0)


@pytest.fixture
def tracker(tmp_path, clock):
    with Tracker(tmp_path / "db" / "jobs.db", clock=clock) as t:
        yield t


def test_upsert_inserts_updates_and_skips_duplicates(tracker, clock):
    res = tracker.upsert_jobs([job(1), job(2)])
    assert (res.inserted, res.updated, res.duplicates) == (2, 0, 0)

    clock.day = DAY0 + dt.timedelta(days=1)
    tracker.set_status(1, "prepared")
    res = tracker.upsert_jobs([job(1, description="new text"), job(3, title="Data Intern 2")])
    assert (res.inserted, res.updated, res.duplicates) == (0, 1, 1)

    rec = tracker.get(1)
    assert rec.job.description == "new text"
    assert rec.status == "prepared"  # user state survives refresh
    assert rec.first_seen == DAY0.isoformat()
    assert rec.last_seen == clock.day.isoformat()
    assert len(tracker.jobs()) == 2


def test_new_jobs_start_as_new_with_history(tracker):
    tracker.upsert_jobs([job(1)])
    rec = tracker.get(1)
    assert rec.status == "new"
    assert [e.to_status for e in tracker.history(1)] == ["new"]
    assert rec.job.uid == "lever:paylane:1"


def test_applied_schedules_followup_after_7_days(tracker, clock):
    tracker.upsert_jobs([job(1)])
    rec = tracker.set_status(1, "applied", note="sent via company site")
    due = (DAY0 + dt.timedelta(days=FOLLOWUP_DAYS)).isoformat()
    assert rec.applied_at == DAY0.isoformat()
    assert rec.next_followup_at == due

    assert tracker.due_followups() == []
    clock.day = DAY0 + dt.timedelta(days=6)
    assert tracker.due_followups() == []
    clock.day = DAY0 + dt.timedelta(days=7)
    assert [r.id for r in tracker.due_followups()] == [1]


def test_follow_up_reschedules_and_interview_clears(tracker, clock):
    tracker.upsert_jobs([job(1)])
    tracker.set_status(1, "applied")
    clock.day = DAY0 + dt.timedelta(days=8)
    rec = tracker.set_status(1, "follow-up")  # alias
    assert rec.status == "follow_up"
    assert rec.next_followup_at == (clock.day + dt.timedelta(days=7)).isoformat()
    assert rec.applied_at == DAY0.isoformat()  # original application date kept
    assert tracker.due_followups() == []

    rec = tracker.set_status(1, "entrevista")
    assert rec.status == "interview"
    assert rec.next_followup_at is None
    clock.day = DAY0 + dt.timedelta(days=60)
    assert tracker.due_followups() == []
    history = [(e.from_status, e.to_status) for e in tracker.history(1)]
    assert history == [
        (None, "new"),
        ("new", "applied"),
        ("applied", "follow_up"),
        ("follow_up", "interview"),
    ]


def test_rejected_has_no_followup(tracker):
    tracker.upsert_jobs([job(1)])
    tracker.set_status(1, "applied")
    assert tracker.set_status(1, "rechazado").next_followup_at is None


def test_explicit_application_date(tracker):
    tracker.upsert_jobs([job(1)])
    rec = tracker.set_status(1, "applied", when=dt.date(2026, 9, 1))
    assert rec.next_followup_at == "2026-09-08"
    assert [r.id for r in tracker.due_followups()] == [1]  # overdue


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("nuevo", "new"),
        ("PREPARADO", "prepared"),
        ("aplicado", "applied"),
        ("follow-up", "follow_up"),
        ("follow up", "follow_up"),
        ("interview", "interview"),
        ("rechazado", "rejected"),
        ("descartada", "discarded"),
    ],
)
def test_status_aliases(raw, expected):
    assert normalize_status(raw) == expected


def test_invalid_status_and_id(tracker):
    tracker.upsert_jobs([job(1)])
    with pytest.raises(TrackerError, match="unknown status"):
        tracker.set_status(1, "hired?")
    with pytest.raises(TrackerError, match="no job"):
        tracker.get(99)


def test_scores_filters_and_counts(tracker):
    tracker.upsert_jobs([job(1), job(2), job(3)])
    tracker.set_score(1, 80, "great")
    tracker.set_score(2, 40, "meh")
    assert [r.id for r in tracker.jobs()] == [1, 2, 3]  # by score desc, unscored last
    assert [r.id for r in tracker.jobs(min_score=50)] == [1]
    assert [r.id for r in tracker.jobs(unscored_only=True)] == [3]
    tracker.set_status(2, "rejected")
    assert [r.id for r in tracker.jobs(status=["new"])] == [1, 3]
    assert tracker.counts() == {
        "new": 2,
        "prepared": 0,
        "applied": 0,
        "follow_up": 0,
        "interview": 0,
        "rejected": 1,
        "discarded": 0,
    }


def test_notes_and_prepared_dir(tracker):
    tracker.upsert_jobs([job(1)])
    tracker.add_note(1, "Referral from Ana")
    tracker.set_prepared_dir(1, "output/0001")
    rec = tracker.get(1)
    assert rec.notes == f"{DAY0.isoformat()}: Referral from Ana"
    assert rec.prepared_dir == "output/0001"


def test_export_csv(tracker, tmp_path):
    tracker.upsert_jobs([job(1), job(2, title="Análisis de datos, prácticas")])
    tracker.set_score(1, 77, "internship ✓ · remote ✓")
    tracker.set_status(1, "applied")
    out = tmp_path / "exports" / "apps.csv"
    assert tracker.export_csv(out) == 2
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["id"] == "1"
    assert rows[0]["status"] == "applied"
    assert rows[0]["score_explanation"] == "internship ✓ · remote ✓"
    assert rows[0]["next_followup_at"] == "2026-10-14"
    assert rows[1]["title"] == "Análisis de datos, prácticas"
    assert tracker.export_csv(tmp_path / "applied.csv", status="applied") == 1


def test_persistence_across_connections(tmp_path, clock):
    path = tmp_path / "jobs.db"
    with Tracker(path, clock=clock) as t:
        t.upsert_jobs([job(1)])
        t.set_status(1, "applied")
    with Tracker(path, clock=clock) as t:
        assert t.get(1).status == "applied"


# ------------------------------------------------------------- v2: meta, timestamps


class Ticker:
    """Deterministic UTC clock that advances one second per call."""

    def __init__(self) -> None:
        self.t = dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        self.t += dt.timedelta(seconds=1)
        return self.t


def test_meta_roundtrip(tracker):
    assert tracker.get_meta("profile_fingerprint") is None
    tracker.set_meta("profile_fingerprint", "abc")
    tracker.set_meta("profile_fingerprint", "def")
    assert tracker.get_meta("profile_fingerprint") == "def"


def test_first_seen_after(tmp_path, clock):
    with Tracker(tmp_path / "t.db", clock=clock, now=Ticker()) as t:
        t.upsert_jobs([job(1)])
        mark = t.timestamp()
        t.upsert_jobs([job(1, description="refresh"), job(2)])
        assert [r.id for r in t.first_seen_after(mark)] == [2]
        assert {r.id for r in t.first_seen_after(None)} == {1, 2}
        assert t.get(1).first_seen_at < mark < t.get(2).first_seen_at


def test_migrates_v1_database(tmp_path):
    """A tracker created by v0.1 (no first_seen_at, no meta table) keeps working."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE,
            dedup_key TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
            company TEXT NOT NULL, company_slug TEXT NOT NULL, sector TEXT,
            title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
            locations TEXT NOT NULL DEFAULT '[]', workplace_type TEXT NOT NULL DEFAULT 'unknown',
            employment_type TEXT NOT NULL DEFAULT 'unknown', department TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', posted_at TEXT, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, score INTEGER, score_explanation TEXT, scored_at TEXT,
            status TEXT NOT NULL DEFAULT 'new', status_changed_at TEXT NOT NULL,
            applied_at TEXT, next_followup_at TEXT, prepared_dir TEXT,
            notes TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO jobs (uid, dedup_key, source, external_id, company, company_slug, title,
                          first_seen, last_seen, status_changed_at, score)
        VALUES ('lever:old:1', 'k', 'lever', '1', 'Old', 'old', 'Old intern',
                '2026-10-01', '2026-10-01', '2026-10-01', 70);
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()
    with Tracker(path, now=Ticker()) as t:
        old = t.get(1)
        assert old.score == 70 and old.first_seen_at is None
        assert t.first_seen_after(None) == []  # pre-existing rows never count as "new"
        t.set_meta("k", "v")
        t.upsert_jobs([job(5)])
        assert [r.job.external_id for r in t.first_seen_after(None)] == ["5"]
        assert t.conn.execute("PRAGMA user_version").fetchone()[0] == 2
