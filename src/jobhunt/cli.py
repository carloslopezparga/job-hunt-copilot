"""Command-line interface.

    jobhunt init                     create profile.yaml from the template
    jobhunt discover                 fetch public job boards into the tracker
    jobhunt add                      track an offer found elsewhere (e.g. a bank's site)
    jobhunt score                    score tracked jobs against your profile
    jobhunt prepare <id> [<id>...]   generate CV + cover letter + LinkedIn note to review
    jobhunt status [<id> [<state>]]  pipeline overview / job detail / update state
    jobhunt followups                applications due for a follow-up
    jobhunt export                   export the tracker to CSV

The copilot never submits applications and never automates LinkedIn.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jobhunt.generator import IncompleteProfileError, prepare_application
from jobhunt.generator.llm import get_polisher
from jobhunt.models import Job
from jobhunt.profile import SECTORS, Profile, ProfileError, load_profile
from jobhunt.scoring import deduplicate, score_job
from jobhunt.sources import FixtureFetcher, HttpFetcher, discover, load_companies
from jobhunt.tracker import STATUSES, Tracker, TrackerError, normalize_status

app = typer.Typer(
    help="Human-in-the-loop internship search copilot. It prepares; you review and send.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

TEMPLATE_NAME = "profile.template.yaml"


@dataclass
class Settings:
    profile: Path
    db: Path
    companies: Path
    out: Path


def _today() -> dt.date:
    """Today's date; JOBHUNT_TODAY=YYYY-MM-DD overrides it (demos and tests)."""
    override = os.environ.get("JOBHUNT_TODAY")
    return dt.date.fromisoformat(override) if override else dt.date.today()


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj


def _tracker(ctx: typer.Context) -> Tracker:
    return Tracker(_settings(ctx).db, clock=_today)


def _profile(ctx: typer.Context) -> Profile:
    try:
        return load_profile(_settings(ctx).profile)
    except ProfileError as exc:
        err.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.callback()
def main(
    ctx: typer.Context,
    profile: Annotated[
        Path, typer.Option(envvar="JOBHUNT_PROFILE", help="Your profile (git-ignored).")
    ] = Path("profile.yaml"),
    db: Annotated[Path, typer.Option(envvar="JOBHUNT_DB", help="SQLite tracker file.")] = Path(
        "data/jobhunt.db"
    ),
    companies: Annotated[
        Path, typer.Option(envvar="JOBHUNT_COMPANIES", help="Companies to monitor.")
    ] = Path("companies.yaml"),
    out: Annotated[
        Path, typer.Option(envvar="JOBHUNT_OUTPUT", help="Where prepared material is written.")
    ] = Path("output"),
) -> None:
    ctx.obj = Settings(profile=profile, db=db, companies=companies, out=out)


# --------------------------------------------------------------------------- init


@app.command()
def init(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Overwrite an existing profile.yaml.")] = False,
) -> None:
    """Create profile.yaml from the commented template and initialise the tracker."""
    settings = _settings(ctx)
    template = Path(TEMPLATE_NAME)
    if not template.exists():
        err.print(f"[red]{TEMPLATE_NAME} not found; run from the repository root.[/red]")
        raise typer.Exit(1)
    if settings.profile.exists() and not force:
        console.print(f"{settings.profile} already exists (use --force to overwrite).")
    else:
        shutil.copyfile(template, settings.profile)
        console.print(f"Created [bold]{settings.profile}[/bold]. Fill in every TODO field.")
    with _tracker(ctx):
        pass
    console.print(f"Tracker ready at {settings.db}.")


# ----------------------------------------------------------------------- discover


@app.command(name="discover")
def discover_cmd(
    ctx: typer.Context,
    company: Annotated[
        list[str] | None, typer.Option("--company", "-c", help="Only these companies.")
    ] = None,
    fixtures: Annotated[
        Path | None,
        typer.Option(help="Read <source>_<slug>.json files from this folder (offline mode)."),
    ] = None,
) -> None:
    """Fetch the public Greenhouse/Lever/Ashby boards listed in companies.yaml."""
    settings = _settings(ctx)
    try:
        companies = load_companies(settings.companies)
    except (OSError, ValueError) as exc:
        err.print(f"[red]Cannot load companies:[/red] {exc}")
        raise typer.Exit(1) from exc
    if company:
        wanted = {c.lower() for c in company}
        companies = [c for c in companies if c.name.lower() in wanted or c.slug in wanted]
        if not companies:
            err.print("[red]No matching companies.[/red]")
            raise typer.Exit(1)

    fetcher = FixtureFetcher(fixtures) if fixtures else HttpFetcher()
    console.print(f"Fetching {len(companies)} boards{' (offline)' if fixtures else ''}…")
    result = discover(companies, fetcher)
    jobs = deduplicate(result.jobs)
    with _tracker(ctx) as tracker:
        stats = tracker.upsert_jobs(jobs)

    for name, count in sorted(result.counts.items()):
        console.print(f"  [green]✓[/green] {name}: {count} postings")
    for name, error in sorted(result.errors.items()):
        console.print(f"  [yellow]![/yellow] {name}: {error}")
    console.print(
        f"[bold]{stats.inserted} new[/bold], {stats.updated} refreshed, "
        f"{stats.duplicates + len(result.jobs) - len(jobs)} duplicates skipped. "
        "Next: [bold]jobhunt score[/bold]"
    )


# ---------------------------------------------------------------------------- add


@app.command()
def add(
    ctx: typer.Context,
    company: Annotated[str, typer.Option(help="Company name.")],
    title: Annotated[str, typer.Option(help="Job title as posted.")],
    url: Annotated[str, typer.Option(help="Link to the official posting.")],
    sector: Annotated[str, typer.Option(help=f"One of: {', '.join(SECTORS)}.")],
    location: Annotated[str, typer.Option(help="City, country.")] = "",
    workplace: Annotated[str, typer.Option(help="remote | hybrid | onsite | unknown")] = "unknown",
    internship: Annotated[bool, typer.Option(help="Is it an internship?")] = True,
    description_file: Annotated[
        Path | None, typer.Option(help="Text file with the job description (copy-paste).")
    ] = None,
) -> None:
    """Track an offer you found yourself (bank portals, LinkedIn, referrals...)."""
    if sector not in SECTORS:
        err.print(f"[red]Unknown sector '{sector}'. Use one of: {', '.join(SECTORS)}[/red]")
        raise typer.Exit(1)
    if workplace not in ("remote", "hybrid", "onsite", "unknown"):
        err.print("[red]--workplace must be remote, hybrid, onsite or unknown[/red]")
        raise typer.Exit(1)
    description = description_file.read_text(encoding="utf-8") if description_file else ""
    slug = "".join(ch for ch in company.lower() if ch.isalnum()) or "manual"
    job = Job(
        source="manual",
        external_id=dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
        company=company,
        company_slug=slug,
        sector=sector,
        title=title,
        url=url,
        location=location,
        locations=[location] if location else [],
        workplace_type=workplace,
        employment_type="internship" if internship else "unknown",
        description=description,
        posted_at=_today().isoformat(),
    )
    with _tracker(ctx) as tracker:
        stats = tracker.upsert_jobs([job])
        if stats.duplicates:
            console.print("[yellow]Already tracked (same company, title and city).[/yellow]")
            return
        record = next(r for r in tracker.jobs() if r.job.uid == job.uid)
    console.print(f"Added job [bold]#{record.id}[/bold]: {title} — {company}")


# -------------------------------------------------------------------------- score


@app.command()
def score(
    ctx: typer.Context,
    rescore: Annotated[bool, typer.Option(help="Re-score jobs that already have a score.")] = False,
    min_score: Annotated[int, typer.Option(help="Only list jobs at or above this score.")] = 0,
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 25,
) -> None:
    """Score tracked jobs 0-100 against your profile and list the best matches."""
    profile = _profile(ctx)
    today = _today()
    with _tracker(ctx) as tracker:
        pending = tracker.jobs() if rescore else tracker.jobs(unscored_only=True)
        for record in pending:
            result = score_job(record.job, profile, today)
            tracker.set_score(record.id, result.score, result.explanation)
        records = [
            r
            for r in tracker.jobs(status=["new", "prepared"])
            if r.score is not None and r.score >= min_score
        ]
    console.print(f"Scored {len(pending)} job(s).")
    _print_jobs(records[:limit], explain=True)
    threshold = profile.scoring.min_score_to_prepare
    good = [r.id for r in records if r.score is not None and r.score >= threshold and
            r.status == "new"]  # fmt: skip
    if good:
        ids = " ".join(map(str, good[:5]))
        console.print(f"Next: review the top matches, then [bold]jobhunt prepare {ids}[/bold]")


# ------------------------------------------------------------------------ prepare


@app.command()
def prepare(
    ctx: typer.Context,
    ids: Annotated[list[int] | None, typer.Argument(help="Tracker ids to prepare.")] = None,
    top: Annotated[
        int, typer.Option(help="Also prepare the N best 'new' jobs above min_score_to_prepare.")
    ] = 0,
    lang: Annotated[
        str | None, typer.Option(help="en | es (default: auto from the posting/profile).")
    ] = None,
    llm: Annotated[
        str, typer.Option(help="Optional polishing backend: none | anthropic.")
    ] = "none",
    allow_placeholders: Annotated[
        bool, typer.Option(help="Generate even if profile.yaml has TODOs (drafts only).")
    ] = False,
) -> None:
    """Generate CV (PDF), cover letter and LinkedIn note for review. Sends nothing."""
    settings = _settings(ctx)
    profile = _profile(ctx)
    if lang and lang not in ("en", "es"):
        err.print("[red]--lang must be 'en' or 'es'[/red]")
        raise typer.Exit(1)
    try:
        polisher = get_polisher(llm)
    except (ValueError, RuntimeError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    with _tracker(ctx) as tracker:
        targets = list(ids or [])
        if top:
            threshold = profile.scoring.min_score_to_prepare
            best = [r.id for r in tracker.jobs(status="new", min_score=threshold)]
            targets += [i for i in best if i not in targets][:top]
        if not targets:
            err.print("Nothing to prepare: pass job ids or --top N (run `jobhunt score` first).")
            raise typer.Exit(1)
        for job_id in targets:
            try:
                record = tracker.get(job_id)
                result = prepare_application(
                    record.job,
                    profile,
                    settings.out,
                    lang=lang,
                    polisher=polisher,
                    record_id=record.id,
                    score=record.score,
                    explanation=record.score_explanation,
                    today=_today(),
                    allow_placeholders=allow_placeholders,
                )
            except (TrackerError, IncompleteProfileError) as exc:
                err.print(f"[red]#{job_id}: {exc}[/red]")
                raise typer.Exit(1) from exc
            tracker.set_prepared_dir(record.id, str(result.directory))
            if record.status == "new":
                tracker.set_status(record.id, "prepared", note=f"material in {result.directory}")
            console.print(
                f"[green]✓[/green] #{record.id} {record.job.title} — {record.job.company} "
                f"\\[{result.lang}] → {result.directory}  "
                f"(letter {result.letter_words} words, note {result.note_chars} chars)"
            )
            for warning in result.warnings:
                console.print(f"    [yellow]![/yellow] {warning}")
    console.print(
        "Review every file, apply yourself on the company site, then run "
        "[bold]jobhunt status <id> applied[/bold]."
    )


# ------------------------------------------------------------------------- status


@app.command()
def status(
    ctx: typer.Context,
    job_id: Annotated[int | None, typer.Argument(help="Job id.")] = None,
    new_status: Annotated[
        str | None,
        typer.Argument(help=f"New state: {', '.join(STATUSES)} (Spanish names accepted)."),
    ] = None,
    note: Annotated[str, typer.Option(help="Note stored with the change.")] = "",
    date: Annotated[
        str | None, typer.Option(help="When it happened (YYYY-MM-DD), default today.")
    ] = None,
    filter_status: Annotated[
        str | None, typer.Option("--filter", help="List only jobs in this state.")
    ] = None,
) -> None:
    """Pipeline overview, one job's detail, or record a state change you made."""
    with _tracker(ctx) as tracker:
        try:
            if job_id is None:
                counts = tracker.counts()
                console.print(" · ".join(f"{s}: [bold]{n}[/bold]" for s, n in counts.items()))
                statuses = (
                    [normalize_status(filter_status)]
                    if filter_status
                    else [s for s in STATUSES if s != "rejected"]
                )
                _print_jobs(tracker.jobs(status=statuses), explain=False)
                return
            if new_status is None:
                _print_detail(tracker, job_id)
                return
            when = dt.date.fromisoformat(date) if date else None
            record = tracker.set_status(job_id, new_status, note=note, when=when)
            if note:
                tracker.add_note(job_id, note)
        except (TrackerError, ValueError) as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    message = (
        f"#{record.id} {record.job.title} — {record.job.company}: [bold]{record.status}[/bold]"
    )
    if record.next_followup_at:
        message += f" (follow up on {record.next_followup_at})"
    console.print(message)


# ---------------------------------------------------------------------- followups


@app.command()
def followups(
    ctx: typer.Context,
    days_ahead: Annotated[int, typer.Option(help="Also show follow-ups due in N days.")] = 0,
) -> None:
    """Applications whose 7-day follow-up reminder is due."""
    day = _today() + dt.timedelta(days=days_ahead)
    with _tracker(ctx) as tracker:
        due = tracker.due_followups(day)
    if not due:
        console.print("No follow-ups due.")
        return
    table = Table(title=f"Follow-ups due by {day.isoformat()}")
    for column in ("id", "company", "title", "status", "applied", "due", "url"):
        table.add_column(column)
    for r in due:
        table.add_row(
            str(r.id), r.job.company, r.job.title, r.status, r.applied_at or "-",
            r.next_followup_at or "-", r.job.url,
        )  # fmt: skip
    console.print(table)
    console.print(
        "Write a short, polite follow-up yourself, then record it with "
        "[bold]jobhunt status <id> follow-up[/bold] (next reminder in 7 days)."
    )


# ------------------------------------------------------------------------- export


@app.command()
def export(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="CSV file to write.")] = Path(
        "output/applications.csv"
    ),
    filter_status: Annotated[str | None, typer.Option("--filter", help="Only this state.")] = None,
) -> None:
    """Export the tracker to CSV (opens in Excel / Google Sheets)."""
    with _tracker(ctx) as tracker:
        try:
            count = tracker.export_csv(path, status=filter_status)
        except TrackerError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    console.print(f"Exported {count} job(s) to {path}")


# ------------------------------------------------------------------------ helpers


def _print_jobs(records, explain: bool) -> None:  # noqa: ANN001
    if not records:
        console.print("No jobs to show.")
        return
    table = Table(show_lines=False)
    table.add_column("id", justify="right")
    table.add_column("score", justify="right")
    table.add_column("status")
    table.add_column("company")
    table.add_column("title")
    table.add_column("location")
    if explain:
        table.add_column("why", overflow="fold")
    for r in records:
        row = [
            str(r.id),
            "-" if r.score is None else str(r.score),
            r.status,
            r.job.company,
            r.job.title,
            r.job.location or r.job.workplace_type,
        ]
        if explain:
            row.append(r.score_explanation or "")
        table.add_row(*row)
    console.print(table)


def _print_detail(tracker: Tracker, job_id: int) -> None:
    r = tracker.get(job_id)
    console.print(f"[bold]#{r.id} {r.job.title} — {r.job.company}[/bold]")
    console.print(f"  {r.job.url}")
    console.print(
        f"  location: {r.job.location or '-'} · {r.job.workplace_type} · {r.job.employment_type}"
    )
    console.print(
        f"  score: {r.score if r.score is not None else '-'}  {r.score_explanation or ''}"
    )
    console.print(f"  status: [bold]{r.status}[/bold] since {r.status_changed_at}")
    if r.applied_at:
        console.print(f"  applied: {r.applied_at} · next follow-up: {r.next_followup_at or '-'}")
    if r.prepared_dir:
        console.print(f"  material: {r.prepared_dir}")
    if r.notes:
        console.print("  notes:\n    " + r.notes.replace("\n", "\n    "))
    console.print("  history:")
    for event in tracker.history(job_id):
        arrow = f"{event.from_status or '∅'} → {event.to_status}"
        console.print(f"    {event.at}  {arrow}  {event.note}")


if __name__ == "__main__":  # pragma: no cover
    app()
