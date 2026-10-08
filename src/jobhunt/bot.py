"""Telegram bot: the main interface of the copilot (`jobhunt bot`).

It long-polls the Bot API, answers ONLY the configured chat (any other chat is ignored
without a reply) and runs a discover + score once a day at a configurable time, so it does
not depend on the Windows Task Scheduler.

Commands: /hoy /madrid /espana /pendientes /stats /buscar /ayuda. Under every offer:
Abrir (link), Preparar (CV + letter), Descartar, Aplicada (status only).

The bot never applies anywhere, never contacts employers and never touches LinkedIn: the
buttons only open links, write files for you to review, or change the status in your own
tracker.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jobhunt.generator import IncompleteProfileError
from jobhunt.location import home_countries
from jobhunt.models import Company
from jobhunt.notify import Button, NotifyError
from jobhunt.pipeline import (
    DiscoverySummary,
    LocationFilter,
    discover_tracked,
    prepare_record,
    score_tracked,
)
from jobhunt.profile import Profile, ProfileError, load_profile
from jobhunt.scoring import assess_location
from jobhunt.sources import Fetcher, HttpFetcher, load_companies
from jobhunt.textutils import fold
from jobhunt.tracker import JobRecord, Tracker, TrackerError

DIGEST_KEY = "bot_last_digest"  # cutoff: jobs first seen after it are "new" for the digest
DAILY_KEY = "bot_last_daily"  # date (YYYY-MM-DD) of the last automatic daily search
SEARCH_KEY = "bot_last_search"  # ISO timestamp of the last search (any trigger)
NEW_WINDOW_DAYS = 1  # /hoy: offers first seen today or yesterday

LEVEL_ES = {
    "home": "Madrid",
    "country": "resto de España",
    "remote": "remoto desde España/UE",
    "abroad": "UE presencial",
    "other": "otro país",
}

HELP = """Comandos:
/hoy - ofertas nuevas que pasan tus filtros
/madrid - mejores ofertas en Madrid
/espana - mejores ofertas en España
/pendientes - aplicadas sin respuesta y follow-ups vencidos
/stats - resumen del embudo
/buscar - descubrir y puntuar ahora
/ayuda - esta ayuda

Bajo cada oferta: Abrir, Preparar (te envío CV y carta para que los revises), Descartar, \
Aplicada (solo cambia el estado). Yo nunca envío candidaturas ni toco LinkedIn: aplicas tú."""


class TelegramClient(Protocol):
    """What the bot needs from Telegram (implemented by notify.TelegramApi and by test fakes)."""

    def get_me(self) -> dict: ...
    def get_updates(self, offset: int | None, timeout: int) -> list[dict]: ...
    def send_message(
        self, chat_id: str, text: str, buttons: list[list[Button]] | None = None
    ) -> int: ...
    def edit_buttons(self, chat_id: str, message_id: int, buttons: list[list[Button]]) -> None: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def send_document(self, chat_id: str, path: str | Path, caption: str = "") -> None: ...


@dataclass
class BotConfig:
    chat_id: str
    profile: Path
    db: Path
    companies: Path
    out: Path
    daily_at: dt.time | None = dt.time(9, 0)  # local time; None disables the daily search
    poll_timeout: int = 30
    max_offers: int = 10  # offers sent per command
    send_delay: float = 0.3  # pause between messages (Telegram allows ~1 msg/s per chat)


def parse_daily_time(value: str) -> dt.time:
    try:
        hour, minute = value.strip().split(":")
        return dt.time(int(hour), int(minute))
    except ValueError as exc:
        raise ValueError(f"invalid time '{value}': use HH:MM, e.g. 09:00") from exc


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="jobhunt-search").start()


@dataclass
class SearchReport:
    summary: DiscoverySummary
    rescored: int
    fresh: list[JobRecord] = field(default_factory=list)  # new matches, best first
    total_matches: int = 0


class Bot:
    def __init__(
        self,
        api: TelegramClient,
        config: BotConfig,
        *,
        today: Callable[[], dt.date] = dt.date.today,
        now: Callable[[], dt.datetime] = dt.datetime.now,
        fetcher: Callable[[], Fetcher] = HttpFetcher,
        background: Callable[[Callable[[], None]], None] = _start_thread,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.cfg = config
        self.today = today
        self.now = now
        self.make_fetcher = fetcher
        self.background = background
        self.sleep = sleep
        self.log = log
        self.offset: int | None = None
        self._searching = threading.Lock()

    # ------------------------------------------------------------------ plumbing

    def _tracker(self) -> Tracker:
        return Tracker(self.cfg.db, clock=self.today)

    def _say(self, text: str, buttons: list[list[Button]] | None = None) -> int:
        return self.api.send_message(self.cfg.chat_id, text, buttons)

    def _pause(self) -> None:
        if self.cfg.send_delay:
            self.sleep(self.cfg.send_delay)

    def _profile(self) -> Profile | None:
        try:
            return load_profile(self.cfg.profile)
        except ProfileError as exc:
            self._say(f"No puedo leer el perfil: {exc}")
            return None

    def _matches(self, tracker: Tracker, profile: Profile, statuses: list[str]) -> list[JobRecord]:
        """Offers at or above the shortlist threshold that pass the run filters, best first."""
        threshold = profile.scoring.min_score_to_prepare
        flt = LocationFilter.from_profile(profile)
        records = tracker.jobs(status=statuses, min_score=threshold)
        return [r for r in records if flt.accepts(r.job, profile)]

    # ------------------------------------------------------------- update dispatch

    def handle_update(self, update: dict) -> None:
        """Handle one Telegram update; anything from another chat is ignored silently."""
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except (NotifyError, OSError) as exc:
            self.log(f"telegram error: {exc}")
        except Exception as exc:  # noqa: BLE001 - the bot must survive any single update
            self.log(f"error handling update {update.get('update_id')}: {exc!r}")
            with contextlib.suppress(NotifyError, OSError):
                self._say(f"Algo ha fallado ({type(exc).__name__}). Mira el log del bot.")

    def _from_my_chat(self, chat: dict | None) -> bool:
        return chat is not None and str(chat.get("id")) == self.cfg.chat_id

    def _on_message(self, message: dict) -> None:
        if not self._from_my_chat(message.get("chat")):
            self.log(f"ignored message from chat {message.get('chat', {}).get('id')}")
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            self._say("Usa /ayuda para ver los comandos.")
            return
        command = fold(text.split()[0].lstrip("/").split("@")[0])
        handler = {
            "start": self.cmd_help,
            "ayuda": self.cmd_help,
            "help": self.cmd_help,
            "hoy": self.cmd_hoy,
            "madrid": self.cmd_madrid,
            "espana": self.cmd_espana,
            "pendientes": self.cmd_pendientes,
            "stats": self.cmd_stats,
            "buscar": self.cmd_buscar,
        }.get(command)
        if handler is None:
            self._say(f"No conozco /{command}. Usa /ayuda.")
            return
        handler()

    def _on_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        if not self._from_my_chat(message.get("chat")):
            self.log(f"ignored button press from chat {message.get('chat', {}).get('id')}")
            return
        action, _, raw_id = (callback.get("data") or "").partition(":")
        callback_id = callback["id"]
        if action == "noop":
            self.api.answer_callback(callback_id)
            return
        if action not in ("prep", "disc", "appl") or not raw_id.isdigit():
            self.api.answer_callback(callback_id, "Botón no reconocido")
            return
        job_id, message_id = int(raw_id), int(message.get("message_id", 0))
        with self._tracker() as tracker:
            try:
                record = tracker.get(job_id)
            except TrackerError:
                self.api.answer_callback(callback_id, "Esa oferta ya no está en el tracker")
                return
            if action == "prep":
                self.api.answer_callback(callback_id, "Preparando…")
                self._prepare(tracker, record)
            else:
                self._change_status(tracker, record, action, callback_id, message_id)

    # ----------------------------------------------------------- button actions

    def _change_status(
        self, tracker: Tracker, record: JobRecord, action: str, callback_id: str, message_id: int
    ) -> None:
        status, label, note = {
            "disc": ("discarded", "Descartada", "descartada desde Telegram"),
            "appl": ("applied", "Aplicada", "marcada como aplicada desde Telegram"),
        }[action]
        if record.status == status:
            self.api.answer_callback(callback_id, f"Ya estaba: {label.lower()}")
            return
        tracker.set_status(record.id, status, note=note)  # status only: nothing is sent anywhere
        text = "Descartada." if action == "disc" else "Marcada como aplicada (follow-up en 7 días)."
        self.api.answer_callback(callback_id, text)
        if message_id:
            self.api.edit_buttons(self.cfg.chat_id, message_id, keyboard(record, label))

    def _prepare(self, tracker: Tracker, record: JobRecord) -> None:
        profile = self._profile()
        if profile is None:
            return
        holes = profile.placeholders()
        if holes:
            self._say(
                f"Tu profile.yaml aún tiene {len(holes)} campos TODO (p. ej. {holes[0]}). "
                "No genero nada hasta que los rellenes: no invento datos tuyos."
            )
            return
        try:
            result = prepare_record(tracker, profile, record, self.cfg.out, self.today())
        except IncompleteProfileError as exc:  # profile edited while the bot was running
            self._say(str(exc))
            return
        job = record.job
        caption = f"{job.company} - {job.title}"
        for key in ("cv", "cover_letter"):
            if key in result.files:
                self.api.send_document(self.cfg.chat_id, result.files[key], caption)
        notes = "\n".join(f"- {w}" for w in result.warnings)
        self._say(
            f"Listo: CV y carta de #{record.id} ({result.lang}). Revísalos, edítalos y aplica "
            f"tú en la web de la empresa; luego pulsa Aplicada.\nCarpeta: {result.directory}"
            + (f"\nAvisos:\n{notes}" if notes else "")
        )

    # ------------------------------------------------------------------ commands

    def cmd_help(self) -> None:
        self._say(HELP)

    def _send_offers(self, records: list[JobRecord], profile: Profile, empty: str) -> None:
        if not records:
            if empty:
                self._say(empty)
            return
        shown = records[: self.cfg.max_offers]
        for i, record in enumerate(shown):
            if i:
                self._pause()
            self._say(format_offer(record, profile), keyboard(record))
        if len(records) > len(shown):
            self._say(f"... y {len(records) - len(shown)} más. Afina con /madrid o /espana.")

    def cmd_hoy(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        since = (self.today() - dt.timedelta(days=NEW_WINDOW_DAYS)).isoformat()
        with self._tracker() as tracker:
            fresh = [r for r in self._matches(tracker, profile, ["new"]) if r.first_seen >= since]
        self._send_offers(
            fresh,
            profile,
            "Hoy no hay ofertas nuevas que pasen tus filtros. Prueba /madrid, /espana o /buscar.",
        )

    def _location_command(self, wanted: str, empty: str) -> None:
        profile = self._profile()
        if profile is None:
            return
        home = home_countries(profile.preferences.locations.preferred)
        with self._tracker() as tracker:
            pool = self._matches(tracker, profile, ["new", "prepared"])
        keep = []
        for record in pool:
            a = assess_location(record.job, profile)
            in_spain = a.level in ("home", "country") or (
                a.level == "remote" and any(p.remote and p.country in home for p in a.places)
            )
            if a.level == "home" if wanted == "madrid" else in_spain:
                keep.append(record)
        self._send_offers(keep, profile, empty)

    def cmd_madrid(self) -> None:
        self._location_command(
            "madrid", "Ahora mismo no hay ofertas en Madrid que pasen tus filtros."
        )

    def cmd_espana(self) -> None:
        self._location_command(
            "espana", "Ahora mismo no hay ofertas en España que pasen tus filtros."
        )

    def cmd_pendientes(self) -> None:
        today = self.today()
        with self._tracker() as tracker:
            due = tracker.due_followups(today)
            waiting = tracker.jobs(status=["applied", "follow_up"])
        if not due and not waiting:
            self._say("No tienes candidaturas pendientes de respuesta.")
            return
        if due:
            lines = [f"Follow-ups vencidos ({len(due)}):"]
            lines += [_pending_line(r, today, show_due=True) for r in due]
            self._say("\n".join(lines))
        due_ids = {r.id for r in due}
        rest = [r for r in waiting if r.id not in due_ids]
        if rest:
            lines = [f"Aplicadas sin respuesta ({len(rest)}):"]
            lines += [_pending_line(r, today) for r in rest]
            self._say("\n".join(lines))

    def cmd_stats(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        with self._tracker() as tracker:
            counts = tracker.counts()
            total = sum(counts.values())
            matches = self._matches(tracker, profile, ["new", "prepared"])
            last = tracker.get_meta(SEARCH_KEY)
        flt = LocationFilter.from_profile(profile)
        madrid = sum(assess_location(r.job, profile).level == "home" for r in matches)
        daily = self.cfg.daily_at.strftime("%H:%M") if self.cfg.daily_at else "desactivada"
        lines = [
            f"Ofertas en el tracker: {total}",
            "Embudo: " + " · ".join(f"{s} {n}" for s, n in counts.items() if n),
            f"Pasan filtros (score >= {profile.scoring.min_score_to_prepare:g}): {len(matches)} "
            f"({madrid} en Madrid)",
            f"Tiers aceptados: {', '.join(flt.tiers) or 'todos'}",
            f"Última búsqueda: {last[:16].replace('T', ' ') if last else 'nunca'}",
            f"Búsqueda diaria: {daily}",
        ]
        self._say("\n".join(lines))

    def cmd_buscar(self) -> None:
        if self._searching.locked():
            self._say("Ya hay una búsqueda en marcha; te aviso al terminar.")
            return
        self._say("Buscando en los tableros y puntuando... tarda un par de minutos.")
        self.background(lambda: self.search("manual"))

    # ------------------------------------------------------------ search + digest

    def run_search(self, profile: Profile) -> SearchReport:
        """discover + score, then collect the offers that are new since the last digest."""
        companies: list[Company] = load_companies(self.cfg.companies)
        with self._tracker() as tracker:
            previous = tracker.get_meta(DIGEST_KEY)
            summary = discover_tracked(tracker, companies, self.make_fetcher())
            cutoff = tracker.timestamp()
            scoring = score_tracked(tracker, profile, self.today())
            matches = {r.id for r in self._matches(tracker, profile, ["new"])}
            fresh = [r for r in tracker.first_seen_after(previous) if r.id in matches]
            tracker.set_meta(DIGEST_KEY, cutoff)
            tracker.set_meta(SEARCH_KEY, self.now().isoformat(timespec="seconds"))
        return SearchReport(summary, scoring.scored, fresh, len(matches))

    def search(self, trigger: str) -> None:
        """Run a search and report to the chat. Safe to call from a background thread."""
        if not self._searching.acquire(blocking=False):
            self._say("Ya hay una búsqueda en marcha.")
            return
        try:
            profile = self._profile()
            if profile is None:
                return
            report = self.run_search(profile)
            s = report.summary
            head = "Búsqueda diaria" if trigger == "daily" else "Búsqueda"
            self._say(
                f"{head} terminada: {len(s.boards_ok)} tableros OK"
                f"{f', {len(s.errors)} con error' if s.errors else ''}; "
                f"{s.inserted} ofertas nuevas, {len(report.fresh)} pasan tus filtros "
                f"(score >= {profile.scoring.min_score_to_prepare:g}). "
                f"En total hay {report.total_matches} candidatas: /madrid /espana."
            )
            self._send_offers(report.fresh, profile, "")
        except Exception as exc:  # noqa: BLE001 - report instead of dying in a thread
            self.log(f"search failed: {exc!r}")
            with contextlib.suppress(NotifyError, OSError):
                self._say(f"La búsqueda ha fallado: {type(exc).__name__}: {exc}")
        finally:
            self._searching.release()

    def maybe_run_daily(self) -> bool:
        """Start today's automatic search if its time has passed and it has not run yet
        (so a PC that was off at the scheduled time catches up when the bot starts)."""
        if self.cfg.daily_at is None:
            return False
        now = self.now()
        if now.time() < self.cfg.daily_at or self._searching.locked():
            return False
        today = now.date().isoformat()
        with self._tracker() as tracker:
            if tracker.get_meta(DAILY_KEY) == today:
                return False
            tracker.set_meta(DAILY_KEY, today)  # mark first: a crash must not loop all day
        self.log("starting the daily search")
        self.background(lambda: self.search("daily"))
        return True

    # ----------------------------------------------------------------- main loop

    def announce(self) -> None:
        daily = (
            f"Búsqueda diaria a las {self.cfg.daily_at:%H:%M}."
            if self.cfg.daily_at
            else "Búsqueda diaria desactivada."
        )
        try:
            self._say(f"job-hunt-copilot activo. {daily} Usa /ayuda.")
        except (NotifyError, OSError) as exc:
            self.log(f"could not send the start-up message: {exc}")

    def poll_once(self) -> int:
        """One long-poll round. Returns the number of updates handled."""
        updates = self.api.get_updates(self.offset, self.cfg.poll_timeout)
        for update in updates:
            self.offset = max(self.offset or 0, int(update["update_id"]) + 1)
            self.handle_update(update)
        return len(updates)

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        delay = 1.0
        while not stop():
            try:
                self.maybe_run_daily()
                self.poll_once()
                delay = 1.0
            except (NotifyError, OSError) as exc:
                self.log(f"polling failed ({exc}); retrying in {delay:.0f}s")
                self.sleep(delay)
                delay = min(delay * 2, 60.0)


# --------------------------------------------------------------------- formatting


def keyboard(record: JobRecord, status_label: str | None = None) -> list[list[Button]]:
    """Buttons under an offer; once it is discarded/applied only the status is shown."""
    row1: list[Button] = []
    if record.job.url.startswith(("http://", "https://")):
        row1.append({"text": "Abrir", "url": record.job.url})
    if status_label:
        return [[*row1, {"text": f"Estado: {status_label}", "callback_data": "noop:0"}]]
    row1.append({"text": "Preparar", "callback_data": f"prep:{record.id}"})
    row2: list[Button] = [
        {"text": "Descartar", "callback_data": f"disc:{record.id}"},
        {"text": "Aplicada", "callback_data": f"appl:{record.id}"},
    ]
    return [row1, row2]


def format_offer(record: JobRecord, profile: Profile) -> str:
    job = record.job
    level = assess_location(job, profile).level
    where = job.location or job.workplace_type
    if level:
        where += f" ({LEVEL_ES[level]})"
    why = (record.score_explanation or "-")[:500]
    return "\n".join(
        [
            f"{record.score}/100 · {job.company}",
            job.title,
            f"Lugar: {where}",
            f"Por qué: {why}",
            f"#{record.id}",
        ]
    )


def _pending_line(record: JobRecord, today: dt.date, show_due: bool = False) -> str:
    applied = dt.date.fromisoformat(record.applied_at) if record.applied_at else None
    ago = f"aplicada hace {(today - applied).days} d" if applied else "aplicada"
    due = f" · follow-up desde {record.next_followup_at}" if show_due else ""
    return f"#{record.id} {record.job.company} - {record.job.title} ({ago}{due})\n{record.job.url}"
