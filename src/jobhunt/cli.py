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

from jobhunt.generator import IncompleteProfileError, PreparedApplication, prepare_application
from jobhunt.generator.llm import Polisher, get_polisher
from jobhunt.models import Job
from jobhunt.notify import TelegramNotifier, notify_offers
from jobhunt.pipeline import LocationFilter, apply_filter, score_tracked
from jobhunt.profile import SECTORS, Profile, ProfileError, load_profile
from jobhunt.scoring import assess_location, deduplicate
from jobhunt.shortlist import render_markdown, write_markdown
from jobhunt.sources import FixtureFetcher, HttpFetcher, discover, load_companies
from jobhunt.sources.check import check_board, qualifies
from jobhunt.sources.check import promote as promote_companies
from jobhunt.tracker import STATUSES, JobRecord, Tracker, TrackerError, normalize_status

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
    with _tracker(ctx) as tracker:
        _discover_into(ctx, tracker, company, fixtures, verbose=True)
    console.print("Next: [bold]jobhunt score[/bold]")


def _discover_into(
    ctx: typer.Context,
    tracker: Tracker,
    company: list[str] | None,
    fixtures: Path | None,
    verbose: bool,
) -> None:
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
    stats = tracker.upsert_jobs(jobs)

    if verbose:
        for name, count in sorted(result.counts.items()):
            console.print(f"  [green]✓[/green] {name}: {count} postings")
    for name, error in sorted(result.errors.items()):
        console.print(f"  [yellow]![/yellow] {name}: {error}")
    console.print(
        f"[bold]{stats.inserted} new[/bold], {stats.updated} refreshed, "
        f"{stats.duplicates + len(result.jobs) - len(jobs)} duplicates skipped "
        f"({len(result.counts)} boards OK, {len(result.errors)} failed)."
    )


@app.command(name="check-boards")
def check_boards(
    ctx: typer.Context,
    file: Annotated[
        Path | None, typer.Option(help="Companies file to check (default: --companies).")
    ] = None,
    promote: Annotated[
        Path | None,
        typer.Option(help="Append boards that pass to this file (e.g. companies.yaml)."),
    ] = None,
    min_eu_jobs: Annotated[
        int, typer.Option(help="Postings in Spain/EU/no-visa places a board needs to pass.")
    ] = 1,
    fixtures: Annotated[Path | None, typer.Option(help="Offline mode (tests/demos).")] = None,
) -> None:
    """Verify boards against the live APIs: 404s, empty boards, and how many postings
    are in Spain or elsewhere in the EU. With --promote, add only boards that pass."""
    path = file or _settings(ctx).companies
    try:
        companies = load_companies(path)
    except (OSError, ValueError) as exc:
        err.print(f"[red]Cannot load companies:[/red] {exc}")
        raise typer.Exit(1) from exc
    fetcher = FixtureFetcher(fixtures) if fixtures else HttpFetcher()
    console.print(f"Checking {len(companies)} boards from {path}…")
    checks = [check_board(c, fetcher) for c in companies]

    table = Table()
    for column in ("company", "board", "status", "result"):
        table.add_column(column, overflow="fold")
    style = {"ok": "green", "empty": "yellow", "not_found": "red", "error": "red"}
    for chk in checks:
        c = chk.company
        status = chk.status
        if status == "ok" and not qualifies(chk, min_eu_jobs):
            status = "ok, no EU jobs"
        table.add_row(c.name, f"{c.ats}/{c.slug}", f"[{style[chk.status]}]{status}[/]",
                      chk.summary)  # fmt: skip
    console.print(table)
    passed = [chk for chk in checks if qualifies(chk, min_eu_jobs)]
    console.print(
        f"[bold]{len(passed)} pass[/bold] (>= {min_eu_jobs} posting(s) in Spain/EU), "
        f"{len(checks) - len(passed)} fail: "
        + ", ".join(f"{sum(c.status == s for c in checks)} {s}" for s in style)
    )
    if promote:
        added = promote_companies(passed, promote, _today())
        console.print(
            f"Added {len(added)} compan{'y' if len(added) == 1 else 'ies'} to {promote}; "
            f"discarded {len(checks) - len(passed)}; "
            f"{len(passed) - len(added)} already present."
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
    rescore: Annotated[
        bool, typer.Option(help="Re-score every job (done automatically if profile.yaml changed).")
    ] = False,
    min_score: Annotated[int, typer.Option(help="Only list jobs at or above this score.")] = 0,
    location: Annotated[
        str | None,
        typer.Option(
            help="Only jobs with a place matching this, e.g. Madrid, Spain, 'Remote - EU'."
        ),
    ] = None,
    no_visa: Annotated[
        bool, typer.Option("--no-visa", help="Hide jobs that need a visa you do not have.")
    ] = False,
    remote_only: Annotated[
        bool, typer.Option("--remote-only", help="Only jobs that can be done remotely.")
    ] = False,
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 25,
) -> None:
    """Score tracked jobs 0-100 against your profile and list the best matches."""
    profile = _profile(ctx)
    flt = LocationFilter(text=location, no_visa=no_visa, remote_only=remote_only)
    with _tracker(ctx) as tracker:
        run = score_tracked(tracker, profile, _today(), force=rescore)
        records = [
            r
            for r in tracker.jobs(status=["new", "prepared"])
            if r.score is not None and r.score >= min_score
        ]
    records = apply_filter(records, profile, flt)
    if run.reason == "profile changed":
        console.print("profile.yaml (or the scoring rules) changed: re-scored every job.")
    console.print(f"Scored {run.scored} job(s). Showing {min(limit, len(records))} of "
                  f"{len(records)} matching job(s).")  # fmt: skip
    _print_jobs(records[:limit], explain=True)
    threshold = profile.scoring.min_score_to_prepare
    good = [r.id for r in records if r.score is not None and r.score >= threshold and
            r.status == "new"]  # fmt: skip
    if good:
        ids = " ".join(map(str, good[:5]))
        console.print(f"Next: review the top matches, then [bold]jobhunt prepare {ids}[/bold]")


@app.command()
def locations(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="Rows to show.")] = 60,
    tier: Annotated[
        str | None, typer.Option(help="Only this tier: preferred, acceptable, other, visa...")
    ] = None,
) -> None:
    """List the location strings in your tracker and how they are classified.

    Use it to spot misclassified places (and to copy real examples into tests).
    """
    profile = _profile(ctx)
    counts: dict[str, list] = {}
    with _tracker(ctx) as tracker:
        for record in tracker.jobs():
            key = " | ".join(dict.fromkeys([record.job.location, *record.job.locations]))
            entry = counts.setdefault(key, [0, record.job])
            entry[0] += 1
    rows = []
    for key, (count, job) in counts.items():
        assessment = assess_location(job, profile)
        if tier and assessment.tier != tier:
            continue
        rows.append((count, key or "(none)", assessment.tier, assessment.label()))
    rows.sort(key=lambda r: (-r[0], r[1]))
    table = Table()
    for column in ("jobs", "location as posted", "tier", "interpretation"):
        table.add_column(column, overflow="fold")
    for count, key, t, label in rows[:limit]:
        table.add_row(str(count), key, t, label)
    console.print(table)
    console.print(f"{len(rows)} distinct location strings.")


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
                _prepare_one(ctx, tracker, profile, record, lang, polisher, allow_placeholders)
            except (TrackerError, IncompleteProfileError) as exc:
                err.print(f"[red]#{job_id}: {exc}[/red]")
                raise typer.Exit(1) from exc
    console.print(
        "Review every file, apply yourself on the company site, then run "
        "[bold]jobhunt status <id> applied[/bold]."
    )


def _prepare_one(
    ctx: typer.Context,
    tracker: Tracker,
    profile: Profile,
    record: JobRecord,
    lang: str | None,
    polisher: Polisher,
    allow_placeholders: bool = False,
) -> PreparedApplication:
    result = prepare_application(
        record.job,
        profile,
        _settings(ctx).out,
        lang=lang,
        polisher=polisher,
        record_id=record.id,
        score=record.score,
        explanation=record.score_explanation,
        today=_today(),
        allow_placeholders=allow_placeholders,
    )
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
    return result


# ---------------------------------------------------------------------------- run

LAST_RUN_KEY = "last_run_cutoff"


@app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    prepare_: Annotated[
        bool, typer.Option("--prepare", help="Generate CV, letter and LinkedIn note for each.")
    ] = False,
    notify: Annotated[
        str | None, typer.Option(help="Send the shortlist to: telegram (env vars required).")
    ] = None,
    export: Annotated[
        Path | None, typer.Option(help="Save the shortlist as markdown (e.g. shortlist.md).")
    ] = None,
    lang: Annotated[str | None, typer.Option(help="en | es for --prepare (default: auto).")] = None,
    llm: Annotated[
        str, typer.Option(help="Optional polishing for --prepare: none | anthropic.")
    ] = "none",
    fixtures: Annotated[Path | None, typer.Option(help="Offline mode (tests/demos).")] = None,
) -> None:
    """Daily routine: discover + score, then show only the offers that are NEW since the
    last run, scored >= min_score_to_prepare and passing the profile's location filters.
    Optionally prepare material, export a markdown shortlist and notify you on Telegram.
    Nothing is ever sent to employers."""
    profile = _profile(ctx)
    if notify not in (None, "telegram"):
        err.print("[red]--notify supports only 'telegram'[/red]")
        raise typer.Exit(1)
    if lang and lang not in ("en", "es"):
        err.print("[red]--lang must be 'en' or 'es'[/red]")
        raise typer.Exit(1)
    threshold = profile.scoring.min_score_to_prepare

    with _tracker(ctx) as tracker:
        previous = tracker.get_meta(LAST_RUN_KEY)
        _discover_into(ctx, tracker, None, fixtures, verbose=False)
        cutoff = tracker.timestamp()
        scoring = score_tracked(tracker, profile, _today())
        if scoring.reason == "profile changed":
            console.print("profile.yaml (or the scoring rules) changed: re-scored every job.")
        fresh = [r for r in tracker.first_seen_after(previous)
                 if r.score is not None and r.score >= threshold]  # fmt: skip
        shortlist = apply_filter(fresh, profile, LocationFilter.from_profile(profile))
        if previous is None:
            console.print("First run: jobs already in the tracker before it are not shown.")
        console.print(
            f"[bold]{len(shortlist)} new offer(s)[/bold] with score >= {threshold:g} "
            f"(of {len(tracker.first_seen_after(previous))} new postings)."
        )
        _print_jobs(shortlist, explain=True)

        prepared: dict[int, PreparedApplication] = {}
        if prepare_ and shortlist:
            holes = profile.placeholders()
            if holes:
                console.print(
                    f"[yellow]profile.yaml still has {len(holes)} TODO field(s) "
                    f"(e.g. {holes[0]}): nothing was generated. Fill them in first.[/yellow]"
                )
            else:
                try:
                    polisher = get_polisher(llm)
                except (ValueError, RuntimeError) as exc:
                    err.print(f"[red]{exc}[/red]")
                    raise typer.Exit(1) from exc
                for record in shortlist:
                    prepared[record.id] = _prepare_one(
                        ctx, tracker, profile, record, lang, polisher
                    )

        if export:
            dirs = {i: p.directory for i, p in prepared.items()}
            content = render_markdown(shortlist, dt.datetime.now(), threshold, dirs)
            console.print(f"Shortlist saved to {write_markdown(export, content)}")

        if notify == "telegram":
            _notify_telegram(shortlist, prepared)

        tracker.set_meta(LAST_RUN_KEY, cutoff)


def _notify_telegram(shortlist: list[JobRecord], prepared: dict[int, PreparedApplication]) -> None:
    notifier = TelegramNotifier.from_env()
    if notifier is None:
        console.print(
            "[yellow]TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: no notification sent.[/yellow]"
        )
        return
    if not shortlist:
        console.print("Nothing new to notify.")
        return
    attachments = {
        i: [p.files[k] for k in ("cv", "cover_letter") if k in p.files] for i, p in prepared.items()
    }
    errors = notify_offers(notifier, shortlist, attachments)
    sent = len(shortlist) - len(errors)
    console.print(f"Telegram: notified {sent}/{len(shortlist)} offer(s).")
    for error in errors:
        console.print(f"  [yellow]![/yellow] {error}")


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
