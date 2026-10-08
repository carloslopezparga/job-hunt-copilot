"""SQLite application tracker: pipeline states, history, follow-up reminders, CSV export.

States: new -> prepared -> applied -> follow_up -> interview / rejected.
Marking a job as ``applied`` (or logging a ``follow_up``) schedules the next
follow-up reminder ``FOLLOWUP_DAYS`` later. The tracker only records what the
user did; it never contacts anyone.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from jobhunt.models import Job
from jobhunt.scoring import dedup_key

FOLLOWUP_DAYS = 7
STATUSES = ("new", "prepared", "applied", "follow_up", "interview", "rejected")
STATUS_ALIASES = {
    "nuevo": "new",
    "preparado": "prepared",
    "aplicado": "applied",
    "follow-up": "follow_up",
    "followup": "follow_up",
    "seguimiento": "follow_up",
    "entrevista": "interview",
    "rechazado": "rejected",
}
OPEN_FOLLOWUP_STATUSES = ("applied", "follow_up")
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    uid               TEXT NOT NULL UNIQUE,
    dedup_key         TEXT NOT NULL,
    source            TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    company           TEXT NOT NULL,
    company_slug      TEXT NOT NULL,
    sector            TEXT,
    title             TEXT NOT NULL,
    url               TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    locations         TEXT NOT NULL DEFAULT '[]',
    workplace_type    TEXT NOT NULL DEFAULT 'unknown',
    employment_type   TEXT NOT NULL DEFAULT 'unknown',
    department        TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    posted_at         TEXT,
    first_seen        TEXT NOT NULL,
    first_seen_at     TEXT,
    last_seen         TEXT NOT NULL,
    score             INTEGER,
    score_explanation TEXT,
    scored_at         TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    status_changed_at TEXT NOT NULL,
    applied_at        TEXT,
    next_followup_at  TEXT,
    prepared_dir      TEXT,
    notes             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
"""

CSV_COLUMNS = [
    "id",
    "company",
    "title",
    "location",
    "workplace_type",
    "employment_type",
    "sector",
    "score",
    "score_explanation",
    "status",
    "status_changed_at",
    "applied_at",
    "next_followup_at",
    "url",
    "source",
    "posted_at",
    "first_seen",
    "prepared_dir",
    "notes",
]


class TrackerError(ValueError):
    pass


def normalize_status(value: str) -> str:
    key = value.strip().lower().replace(" ", "_")
    key = STATUS_ALIASES.get(key, STATUS_ALIASES.get(key.replace("_", "-"), key))
    if key not in STATUSES:
        raise TrackerError(f"unknown status '{value}'. Valid: {', '.join(STATUSES)}")
    return key


@dataclass
class JobRecord:
    id: int
    job: Job
    score: int | None
    score_explanation: str | None
    status: str
    status_changed_at: str
    first_seen: str
    first_seen_at: str | None
    last_seen: str
    applied_at: str | None
    next_followup_at: str | None
    prepared_dir: str | None
    notes: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JobRecord:
        job = Job(
            source=row["source"],
            external_id=row["external_id"],
            company=row["company"],
            company_slug=row["company_slug"],
            sector=row["sector"],
            title=row["title"],
            url=row["url"],
            location=row["location"],
            locations=json.loads(row["locations"] or "[]"),
            workplace_type=row["workplace_type"],
            employment_type=row["employment_type"],
            department=row["department"],
            description=row["description"],
            posted_at=row["posted_at"],
        )
        return cls(
            id=row["id"],
            job=job,
            score=row["score"],
            score_explanation=row["score_explanation"],
            status=row["status"],
            status_changed_at=row["status_changed_at"],
            first_seen=row["first_seen"],
            first_seen_at=row["first_seen_at"],
            last_seen=row["last_seen"],
            applied_at=row["applied_at"],
            next_followup_at=row["next_followup_at"],
            prepared_dir=row["prepared_dir"],
            notes=row["notes"],
        )


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0


@dataclass
class Event:
    at: str
    from_status: str | None
    to_status: str
    note: str


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Tracker:
    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], dt.date] | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or dt.date.today
        self.now = now or _utc_now
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        has_jobs = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if not has_jobs:
            return
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "first_seen_at" not in columns:  # v1 -> v2
            self.conn.execute("ALTER TABLE jobs ADD COLUMN first_seen_at TEXT")

    # -- key/value metadata ------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def timestamp(self) -> str:
        """Current UTC time as a sortable ISO string (microsecond precision)."""
        return self.now().astimezone(dt.UTC).isoformat(timespec="microseconds")

    # -- context manager ---------------------------------------------------------
    def __enter__(self) -> Tracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _today(self) -> str:
        return self.clock().isoformat()

    # -- jobs --------------------------------------------------------------------
    def upsert_jobs(self, jobs: Iterable[Job]) -> UpsertResult:
        """Insert new jobs, refresh known ones, skip duplicates of jobs already tracked.

        User-owned fields (status, notes, dates) are never overwritten.
        """
        result = UpsertResult()
        today = self._today()
        seen_at = self.timestamp()
        with self.conn:
            for job in jobs:
                key = dedup_key(job)
                existing = self.conn.execute(
                    "SELECT id FROM jobs WHERE uid = ?", (job.uid,)
                ).fetchone()
                if existing:
                    self.conn.execute(
                        """UPDATE jobs SET title=?, url=?, location=?, locations=?,
                               workplace_type=?, employment_type=?, department=?,
                               description=?, posted_at=?, sector=?, dedup_key=?, last_seen=?
                           WHERE id=?""",
                        (
                            job.title,
                            job.url,
                            job.location,
                            json.dumps(job.locations),
                            job.workplace_type,
                            job.employment_type,
                            job.department,
                            job.description,
                            job.posted_at,
                            job.sector,
                            key,
                            today,
                            existing["id"],
                        ),
                    )
                    result.updated += 1
                    continue
                twin = self.conn.execute(
                    "SELECT id FROM jobs WHERE dedup_key = ?", (key,)
                ).fetchone()
                if twin:
                    self.conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (today, twin["id"]))
                    result.duplicates += 1
                    continue
                cur = self.conn.execute(
                    """INSERT INTO jobs (uid, dedup_key, source, external_id, company,
                           company_slug, sector, title, url, location, locations,
                           workplace_type, employment_type, department, description,
                           posted_at, first_seen, first_seen_at, last_seen, status,
                           status_changed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', ?)""",
                    (
                        job.uid,
                        key,
                        job.source,
                        job.external_id,
                        job.company,
                        job.company_slug,
                        job.sector,
                        job.title,
                        job.url,
                        job.location,
                        json.dumps(job.locations),
                        job.workplace_type,
                        job.employment_type,
                        job.department,
                        job.description,
                        job.posted_at,
                        today,
                        seen_at,
                        today,
                        today,
                    ),
                )
                self._log(cur.lastrowid, None, "new", "discovered")
                result.inserted += 1
        return result

    def get(self, job_id: int) -> JobRecord:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise TrackerError(f"no job with id {job_id}")
        return JobRecord.from_row(row)

    def jobs(
        self,
        status: str | Iterable[str] | None = None,
        min_score: float | None = None,
        unscored_only: bool = False,
    ) -> list[JobRecord]:
        clauses, params = [], []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            statuses = [normalize_status(s) for s in statuses]
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params += statuses
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        if unscored_only:
            clauses.append("score IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY score IS NULL, score DESC, id", params
        ).fetchall()
        return [JobRecord.from_row(r) for r in rows]

    def first_seen_after(self, timestamp: str | None) -> list[JobRecord]:
        """Jobs first inserted after ``timestamp`` (all timestamped jobs if None)."""
        if timestamp is None:
            sql, params = "SELECT * FROM jobs WHERE first_seen_at IS NOT NULL", ()
        else:
            sql, params = "SELECT * FROM jobs WHERE first_seen_at > ?", (timestamp,)
        rows = self.conn.execute(f"{sql} ORDER BY score IS NULL, score DESC, id", params)
        return [JobRecord.from_row(r) for r in rows.fetchall()]

    def set_score(self, job_id: int, score: int, explanation: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET score=?, score_explanation=?, scored_at=? WHERE id=?",
                (score, explanation, self._today(), job_id),
            )

    def set_prepared_dir(self, job_id: int, directory: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE jobs SET prepared_dir=? WHERE id=?", (directory, job_id))

    def add_note(self, job_id: int, note: str) -> None:
        record = self.get(job_id)
        notes = f"{record.notes}\n{self._today()}: {note}".strip()
        with self.conn:
            self.conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes, job_id))

    # -- pipeline ----------------------------------------------------------------
    def set_status(
        self, job_id: int, status: str, note: str = "", when: dt.date | None = None
    ) -> JobRecord:
        status = normalize_status(status)
        record = self.get(job_id)
        day = when or self.clock()
        follow = (day + dt.timedelta(days=FOLLOWUP_DAYS)).isoformat()
        updates: dict[str, str | None] = {
            "status": status,
            "status_changed_at": day.isoformat(),
        }
        if status == "applied":
            updates["applied_at"] = record.applied_at or day.isoformat()
            updates["next_followup_at"] = follow
        elif status == "follow_up":
            updates["next_followup_at"] = follow
        elif status in ("interview", "rejected", "new", "prepared"):
            updates["next_followup_at"] = None
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self.conn:
            self.conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?", (*updates.values(), job_id)
            )
            self._log(job_id, record.status, status, note, day.isoformat())
        return self.get(job_id)

    def due_followups(self, on: dt.date | None = None) -> list[JobRecord]:
        day = (on or self.clock()).isoformat()
        rows = self.conn.execute(
            f"""SELECT * FROM jobs
                WHERE status IN ({",".join("?" * len(OPEN_FOLLOWUP_STATUSES))})
                  AND next_followup_at IS NOT NULL AND next_followup_at <= ?
                ORDER BY next_followup_at, id""",
            (*OPEN_FOLLOWUP_STATUSES, day),
        ).fetchall()
        return [JobRecord.from_row(r) for r in rows]

    def history(self, job_id: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT at, from_status, to_status, note FROM events WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [Event(r["at"], r["from_status"], r["to_status"], r["note"]) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        found = {r["status"]: r["n"] for r in rows}
        return {s: found.get(s, 0) for s in STATUSES}

    def export_csv(self, path: str | Path, status: str | None = None) -> int:
        records = self.jobs(status=status)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for r in records:
                writer.writerow(
                    {
                        "id": r.id,
                        "company": r.job.company,
                        "title": r.job.title,
                        "location": r.job.location,
                        "workplace_type": r.job.workplace_type,
                        "employment_type": r.job.employment_type,
                        "sector": r.job.sector or "",
                        "score": "" if r.score is None else r.score,
                        "score_explanation": r.score_explanation or "",
                        "status": r.status,
                        "status_changed_at": r.status_changed_at,
                        "applied_at": r.applied_at or "",
                        "next_followup_at": r.next_followup_at or "",
                        "url": r.job.url,
                        "source": r.job.source,
                        "posted_at": r.job.posted_at or "",
                        "first_seen": r.first_seen,
                        "prepared_dir": r.prepared_dir or "",
                        "notes": r.notes,
                    }
                )
        return len(records)

    def _log(
        self,
        job_id: int | None,
        from_status: str | None,
        to_status: str,
        note: str = "",
        at: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, at, from_status, to_status, note) VALUES (?,?,?,?,?)",
            (job_id, at or self._today(), from_status, to_status, note),
        )
