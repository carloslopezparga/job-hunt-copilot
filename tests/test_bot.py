"""The Telegram bot, driven by a simulated Telegram client (no network at all)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil

import pytest

from jobhunt.bot import (
    DAILY_KEY,
    Bot,
    BotConfig,
    format_offer,
    keyboard,
    parse_daily_time,
)
from jobhunt.models import Job
from jobhunt.notify import NotifyError, TelegramApi
from jobhunt.pipeline import score_tracked
from jobhunt.profile import load_profile
from jobhunt.sources import FixtureFetcher
from jobhunt.tracker import Tracker

CHAT = "4242"
TODAY = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 10, 30)


class FakeTelegram:
    """Stands in for the Telegram Bot API: queues updates, records everything sent."""

    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.messages: list[dict] = []  # {"chat", "text", "buttons", "id"}
        self.documents: list[tuple[str, str, str]] = []
        self.edits: list[tuple[int, list]] = []
        self.answers: list[tuple[str, str]] = []
        self.offsets: list[int | None] = []
        self.fail_polls = 0
        self._next_update = 1

    # -- test helpers
    def say(self, text: str, chat: int | str = CHAT) -> None:
        self._push({"message": {"message_id": 1, "chat": {"id": int(chat)}, "text": text}})

    def press(self, data: str, chat: int | str = CHAT, message_id: int = 77) -> None:
        self._push({
            "callback_query": {
                "id": f"cb{self._next_update}",
                "data": data,
                "message": {"message_id": message_id, "chat": {"id": int(chat)}},
            }
        })  # fmt: skip

    def _push(self, body: dict) -> None:
        self.pending.append({"update_id": self._next_update, **body})
        self._next_update += 1

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]

    # -- TelegramClient
    def get_me(self) -> dict:
        return {"username": "fake_bot"}

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        if self.fail_polls:
            self.fail_polls -= 1
            raise NotifyError("Telegram getUpdates failed: URLError")
        ready = [u for u in self.pending if offset is None or u["update_id"] >= offset]
        self.pending = ready
        return list(ready)

    def send_message(self, chat_id, text, buttons=None):
        if not text.strip():  # Telegram rejects these
            raise NotifyError("Telegram sendMessage failed: Bad Request: message text is empty")
        self.messages.append({"chat": chat_id, "text": text, "buttons": buttons,
                              "id": len(self.messages) + 1})  # fmt: skip
        return len(self.messages)

    def edit_buttons(self, chat_id, message_id, buttons):
        self.edits.append((message_id, buttons))

    def answer_callback(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    def send_document(self, chat_id, path, caption=""):
        self.documents.append((chat_id, str(path), caption))


# ------------------------------------------------------------------------ fixtures


def real_job(name: str) -> Job:
    from tests.conftest import FIXTURES

    data = json.loads((FIXTURES / "real_offers.json").read_text(encoding="utf-8"))["offers"][name]
    return Job.from_dict(data)


def custom_job(external_id: str, title: str, location: str, **changes) -> Job:
    base = Job(
        source="greenhouse", external_id=external_id, company="Northwind", company_slug="nw",
        title=title, url=f"https://jobs.example/{external_id}", sector="ai_tech",
        location=location, locations=[location], workplace_type="unknown",
        employment_type="internship",
        description="Summer 2027 internship. Python, SQL, statistics, machine learning, pandas.",
    )  # fmt: skip
    return dataclasses.replace(base, **changes)


@pytest.fixture
def world(tmp_path, root):
    """A scored tracker + a bot wired to a fake Telegram. Returns (bot, telegram, db path)."""
    profile_path = tmp_path / "profile.yaml"
    shutil.copy(root / "profile.example.yaml", profile_path)
    db = tmp_path / "t.db"
    jobs = [
        real_job("prophero_strategy_intern_madrid"),  # Madrid internship, high score
        custom_job("bcn", "Machine Learning Intern, Summer 2027", "Barcelona, Spain"),
        custom_job(
            "rem", "Machine Learning Intern, Summer 2027", "Remote - Spain", workplace_type="remote"
        ),  # fmt: skip
        real_job("imc_macro_analyst_intern_amsterdam"),  # EU on site
        custom_job("low", "Facilities Intern", "Madrid", description="Cleaning."),  # low score
        real_job("prima_data_scientist_madrid"),  # not an internship: capped
    ]
    with Tracker(db, clock=lambda: TODAY) as tracker:
        tracker.upsert_jobs(jobs)
        score_tracked(tracker, load_profile(profile_path), TODAY)
    tg = FakeTelegram()
    config = BotConfig(
        chat_id=CHAT, profile=profile_path, db=db, companies=root / "companies.yaml",
        out=tmp_path / "output", daily_at=None, send_delay=0,
    )  # fmt: skip
    bot = Bot(
        tg, config, today=lambda: TODAY, now=lambda: NOW,
        fetcher=lambda: FixtureFetcher(root / "tests" / "fixtures"),
        background=lambda fn: fn(), sleep=lambda s: None, log=lambda s: None,
    )  # fmt: skip
    return bot, tg, db


def job_titles(tg: FakeTelegram) -> list[str]:
    return [t.split("\n")[1] for t in tg.texts if "\n#" in t]


def status_of(db, title_part: str) -> str:
    with Tracker(db) as tracker:
        return next(r.status for r in tracker.jobs() if title_part in r.job.title)


# ---------------------------------------------------------------------------- tests


def test_ignores_every_other_chat(world):
    bot, tg, db = world
    tg.say("/hoy", chat=999)
    tg.press("disc:1", chat=999)
    tg.press("appl:1", chat=999)
    bot.poll_once()
    assert tg.messages == [] and tg.answers == [] and tg.edits == []
    assert status_of(db, "Strategy Finance") == "new"
    assert bot.offset == 4  # still consumed, so the queue does not grow


def test_help_lists_every_command(world):
    bot, tg, _ = world
    tg.say("/ayuda")
    bot.poll_once()
    for command in ("/hoy", "/madrid", "/espana", "/pendientes", "/stats", "/buscar", "/ayuda"):
        assert command in tg.texts[0]
    assert "nunca envío candidaturas" in tg.texts[0]


def test_unknown_command_and_plain_text_get_a_hint(world):
    bot, tg, _ = world
    tg.say("/nada")
    tg.say("hola")
    bot.poll_once()
    assert all("/ayuda" in t for t in tg.texts) and len(tg.texts) == 2


def test_hoy_sends_one_message_per_offer_that_passes_the_filters(world):
    bot, tg, _ = world
    tg.say("/hoy")
    bot.poll_once()
    titles = job_titles(tg)
    assert "Strategy Finance Intern, Madrid" in titles
    assert "Facilities Intern" not in titles  # below the score threshold
    assert "Data Scientist" not in titles  # not an internship (capped at 40)
    assert len(tg.messages) == len(titles)  # exactly one message per offer
    scores = [int(t.split("/")[0]) for t in tg.texts]
    assert scores == sorted(scores, reverse=True)


def test_every_offer_has_the_four_buttons(world):
    bot, tg, _ = world
    tg.say("/hoy")
    bot.poll_once()
    first = tg.messages[0]
    flat = [b for row in first["buttons"] for b in row]
    assert [b["text"] for b in flat] == ["Abrir", "Preparar", "Descartar", "Aplicada"]
    assert flat[0]["url"].startswith("https://")
    job_id = first["text"].rsplit("#", 1)[1]
    assert [b["callback_data"] for b in flat[1:]] == [
        f"prep:{job_id}", f"disc:{job_id}", f"appl:{job_id}"
    ]  # fmt: skip


def test_offer_without_a_valid_link_has_no_open_button(world):
    bot, _, db = world
    with Tracker(db) as tracker:
        record = tracker.jobs()[0]
    record = dataclasses.replace(record, job=dataclasses.replace(record.job, url="javascript:x"))
    assert [b["text"] for row in keyboard(record) for b in row] == [
        "Preparar", "Descartar", "Aplicada"
    ]  # fmt: skip


def test_madrid_only_lists_madrid_and_espana_lists_all_of_spain(world):
    bot, tg, _ = world
    tg.say("/madrid")
    bot.poll_once()
    assert job_titles(tg) == ["Strategy Finance Intern, Madrid"]
    tg.messages.clear()
    tg.say("/espana")
    bot.poll_once()
    titles = job_titles(tg)
    assert "Strategy Finance Intern, Madrid" in titles
    assert titles.count("Machine Learning Intern, Summer 2027") == 2  # Barcelona + remote Spain
    assert "Macro Analyst Intern" not in titles  # Amsterdam


def test_accented_command_is_understood(world):
    bot, tg, _ = world
    tg.say("/españa")
    bot.poll_once()
    assert job_titles(tg)


def test_empty_results_say_so(world):
    bot, tg, db = world
    with Tracker(db) as tracker:
        for record in tracker.jobs():
            tracker.set_status(record.id, "discarded")
    for command in ("/hoy", "/madrid", "/espana"):
        tg.say(command)
    bot.poll_once()
    assert len(tg.texts) == 3 and all("no hay" in t.lower() for t in tg.texts)


def test_long_lists_are_capped(world):
    bot, tg, _ = world
    bot.cfg.max_offers = 2
    tg.say("/espana")
    bot.poll_once()
    assert len(tg.messages) == 3 and "1 más" in tg.texts[-1]


def test_pendientes_lists_applied_and_due_followups(world):
    bot, tg, db = world
    with Tracker(db, clock=lambda: TODAY) as tracker:
        ids = {r.job.title: r.id for r in tracker.jobs()}
        tracker.set_status(ids["Strategy Finance Intern, Madrid"], "applied",
                           when=TODAY - dt.timedelta(days=10))  # fmt: skip
        tracker.set_status(
            ids["Macro Analyst Intern"], "applied", when=TODAY - dt.timedelta(days=2)
        )
    tg.say("/pendientes")
    bot.poll_once()
    due, waiting = tg.texts
    assert due.startswith("Follow-ups vencidos (1)") and "PropHero" in due
    assert "hace 10 d" in due
    assert waiting.startswith("Aplicadas sin respuesta (1)") and "IMC" in waiting
    assert "hace 2 d" in waiting


def test_pendientes_when_nothing_is_pending(world):
    bot, tg, _ = world
    tg.say("/pendientes")
    bot.poll_once()
    assert "No tienes candidaturas pendientes" in tg.texts[0]


def test_stats(world):
    bot, tg, _ = world
    tg.say("/stats")
    bot.poll_once()
    text = tg.texts[0]
    assert "Ofertas en el tracker: 6" in text and "new 6" in text
    assert "Pasan filtros" in text and "Búsqueda diaria: desactivada" in text


# ---------------------------------------------------------------------------- buttons


def press_on(world, data: str):
    bot, tg, db = world
    tg.press(data)
    bot.poll_once()
    return tg


def job_id(db, title_part: str) -> int:
    with Tracker(db) as tracker:
        return next(r.id for r in tracker.jobs() if title_part in r.job.title)


def test_descartar_only_changes_the_status(world):
    _, tg, db = world
    jid = job_id(db, "Strategy Finance")
    press_on(world, f"disc:{jid}")
    assert status_of(db, "Strategy Finance") == "discarded"
    assert tg.answers[-1][1] == "Descartada."
    assert tg.documents == [] and tg.messages == []  # nothing else is sent
    _, buttons = tg.edits[-1]
    assert "Descartada" in buttons[0][-1]["text"]
    # a discarded offer is gone from /hoy
    tg.say("/hoy")
    world[0].poll_once()
    assert "Strategy Finance Intern, Madrid" not in job_titles(tg)


def test_aplicada_only_changes_the_status(world):
    _, tg, db = world
    jid = job_id(db, "Strategy Finance")
    press_on(world, f"appl:{jid}")
    with Tracker(db) as tracker:
        record = tracker.get(jid)
    assert record.status == "applied" and record.next_followup_at is not None
    assert tg.documents == [] and tg.messages == []  # no files, no messages: status only
    assert not (world[0].cfg.out).exists()  # nothing was generated either


def test_pressing_a_status_button_twice_is_harmless(world):
    _, tg, db = world
    jid = job_id(db, "Strategy Finance")
    press_on(world, f"appl:{jid}")
    press_on(world, f"appl:{jid}")
    assert "Ya estaba" in tg.answers[-1][1] and len(tg.edits) == 1


def test_preparar_refuses_when_the_profile_has_todo(world, root):
    bot, tg, db = world
    shutil.copy(root / "profile.template.yaml", bot.cfg.profile)
    press_on(world, f"prep:{job_id(db, 'Strategy Finance')}")
    assert tg.documents == []
    assert "TODO" in tg.texts[-1] and "No genero nada" in tg.texts[-1]
    assert not bot.cfg.out.exists()
    assert status_of(db, "Strategy Finance") == "new"


def test_preparar_sends_cv_and_cover_letter(world):
    bot, tg, db = world
    press_on(world, f"prep:{job_id(db, 'Strategy Finance')}")
    names = sorted(path.rsplit("/", 1)[1] for _, path, _ in tg.documents)
    assert names == ["cover_letter_en.txt", "cv_en.pdf"]
    assert all("PropHero" in caption for _, _, caption in tg.documents)
    assert status_of(db, "Strategy Finance") == "prepared"
    assert "aplica tú" in tg.texts[-1]  # the bot only prepares: you apply
    assert tg.answers[-1][1] == "Preparando…"


def test_unknown_or_stale_buttons_do_not_crash(world):
    bot, tg, _ = world
    tg.press("prep:99999")
    tg.press("boom:1")
    tg.press("noop:0")
    bot.poll_once()
    assert [a[1] for a in tg.answers] == ["Esa oferta ya no está en el tracker",
                                          "Botón no reconocido", ""]  # fmt: skip


# ------------------------------------------------------------------------------ /buscar


@pytest.fixture
def searching(world, root, tmp_path):
    bot, tg, db = world
    bot.cfg.companies = root / "tests" / "fixtures" / "companies.sample.yaml"
    return bot, tg, db


def test_buscar_discovers_scores_and_reports(searching):
    bot, tg, db = searching
    tg.say("/buscar")
    bot.poll_once()
    assert "Buscando" in tg.texts[0]
    summary = tg.texts[1]
    assert "Búsqueda terminada" in summary and "tableros OK" in summary
    assert "ofertas nuevas" in summary
    with Tracker(db) as tracker:
        assert len(tracker.jobs()) > 6  # fixture postings were stored
        assert not tracker.jobs(unscored_only=True)  # ...and scored
    assert any(m["buttons"] for m in tg.messages[2:])  # new matches come with buttons


def test_a_second_search_only_reports_what_is_new(searching):
    bot, tg, _ = searching
    bot.search("manual")
    tg.messages.clear()
    bot.search("manual")
    assert job_titles(tg) == [] and "0 pasan tus filtros" in tg.texts[0]
    assert len(tg.texts) == 1  # just the summary: no empty or error message follows


def test_buscar_while_a_search_runs_is_refused(searching):
    bot, tg, _ = searching
    bot._searching.acquire()
    tg.say("/buscar")
    bot.poll_once()
    assert "Ya hay una búsqueda en marcha" in tg.texts[0] and len(tg.texts) == 1


def test_failed_search_is_reported_and_releases_the_lock(searching):
    bot, tg, _ = searching
    bot.cfg.companies = bot.cfg.companies.parent / "missing.yaml"
    bot.search("manual")
    assert "ha fallado" in tg.texts[-1]
    assert not bot._searching.locked()


# -------------------------------------------------------------------------- daily search


def test_daily_search_runs_once_a_day_after_the_configured_time(searching):
    bot, tg, db = searching
    bot.cfg.daily_at = dt.time(9, 0)
    clock = {"now": dt.datetime(2026, 10, 8, 8, 59)}
    bot.now = lambda: clock["now"]

    assert bot.maybe_run_daily() is False  # too early
    clock["now"] = dt.datetime(2026, 10, 8, 9, 0)
    assert bot.maybe_run_daily() is True
    assert "Búsqueda diaria terminada" in tg.texts[0]
    assert bot.maybe_run_daily() is False  # already done today
    with Tracker(db) as tracker:
        assert tracker.get_meta(DAILY_KEY) == "2026-10-08"

    clock["now"] = dt.datetime(2026, 10, 9, 8, 0)
    assert bot.maybe_run_daily() is False
    clock["now"] = dt.datetime(2026, 10, 9, 9, 1)
    assert bot.maybe_run_daily() is True


def test_daily_search_catches_up_when_the_pc_was_off_and_survives_restarts(searching):
    bot, tg, _ = searching
    bot.cfg.daily_at = dt.time(9, 0)
    bot.now = lambda: dt.datetime(2026, 10, 8, 15, 0)  # PC switched on in the afternoon
    assert bot.maybe_run_daily() is True
    restarted = dataclasses.replace(bot.cfg)  # a new process, same database
    other = Bot(tg, restarted, today=bot.today, now=bot.now, fetcher=bot.make_fetcher,
                background=lambda fn: fn(), sleep=lambda s: None, log=lambda s: None)  # fmt: skip
    assert other.maybe_run_daily() is False


def test_daily_search_can_be_disabled(searching):
    bot, _, _ = searching
    bot.cfg.daily_at = None
    assert bot.maybe_run_daily() is False


def test_parse_daily_time():
    assert parse_daily_time("09:00") == dt.time(9, 0)
    assert parse_daily_time(" 7:05 ") == dt.time(7, 5)
    for bad in ("9", "25:00", "ab:cd", ""):
        with pytest.raises(ValueError, match="HH:MM"):
            parse_daily_time(bad)


# ---------------------------------------------------------------------------- main loop


def test_run_forever_polls_with_offset_and_backs_off_on_errors(world):
    bot, tg, _ = world
    sleeps: list[float] = []
    bot.sleep = sleeps.append
    tg.fail_polls = 3
    tg.say("/ayuda")
    rounds = {"n": 0}

    def stop():
        rounds["n"] += 1
        return rounds["n"] > 6

    bot.run_forever(stop)
    assert sleeps == [1.0, 2.0, 4.0]  # exponential backoff, reset after success
    assert tg.offsets[-1] == 2  # update 1 confirmed
    assert sum("Comandos:" in t for t in tg.texts) == 1  # handled exactly once


def test_one_broken_update_does_not_stop_the_bot(world):
    bot, tg, _ = world
    bot.cfg.profile = bot.cfg.profile.parent / "gone.yaml"  # profile.yaml disappears
    tg.say("/hoy")
    tg.say("/ayuda")
    bot.poll_once()
    assert any("No puedo leer el perfil" in t for t in tg.texts)
    assert any("Comandos:" in t for t in tg.texts)


def test_announce_survives_telegram_errors(world):
    bot, tg, _ = world

    def boom(*a, **k):
        raise NotifyError("Telegram sendMessage failed: chat not found")

    tg.send_message = boom
    bot.announce()  # must not raise


def test_offer_message_is_readable(world):
    bot, _, db = world
    with Tracker(db) as tracker:
        record = next(r for r in tracker.jobs() if "Strategy Finance" in r.job.title)
    profile = load_profile(bot.cfg.profile)
    text = format_offer(record, profile)
    assert text.splitlines()[0].startswith(f"{record.score}/100 · PropHero")
    assert "Madrid (Madrid)" in text and f"#{record.id}" in text


# ------------------------------------------------------------------------- Telegram API


class RecordingHttp:
    def __init__(self, response=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = response or {"ok": True, "result": {"message_id": 9}}

    def post(self, url, body, headers):
        self.calls.append((url, json.loads(body) if headers["Content-Type"] == "application/json"
                           else {"raw": body}))  # fmt: skip
        return self.response


TOKEN = "123456:SECRET-token"


def test_api_sends_messages_with_inline_buttons():
    http = RecordingHttp()
    api = TelegramApi(TOKEN, http)
    buttons = [[{"text": "Abrir", "url": "https://x"}], [{"text": "Hi", "callback_data": "a:1"}]]
    assert api.send_message("42", "hola", buttons) == 9
    url, payload = http.calls[0]
    assert url.endswith("/sendMessage") and payload["chat_id"] == "42"
    assert payload["reply_markup"] == {"inline_keyboard": buttons}


def test_api_long_polls_with_offset_and_only_the_updates_it_needs():
    http = RecordingHttp({"ok": True, "result": [{"update_id": 5}]})
    api = TelegramApi(TOKEN, http)
    assert api.get_updates(5, 30) == [{"update_id": 5}]
    payload = http.calls[0][1]
    assert payload["offset"] == 5 and payload["timeout"] == 30
    assert set(payload["allowed_updates"]) == {"message", "callback_query"}
    api.get_updates(None, 0)
    assert "offset" not in http.calls[1][1]


def test_api_other_calls():
    http = RecordingHttp({"ok": True, "result": True})
    api = TelegramApi(TOKEN, http)
    api.edit_buttons("42", 7, [[{"text": "x", "callback_data": "noop:0"}]])
    api.answer_callback("cb1", "listo")
    assert [c[0].rsplit("/", 1)[1] for c in http.calls] == [
        "editMessageReplyMarkup", "answerCallbackQuery"
    ]  # fmt: skip
    assert http.calls[1][1] == {"callback_query_id": "cb1", "text": "listo"}


def test_api_errors_never_leak_the_token():
    api = TelegramApi(TOKEN, RecordingHttp({"ok": False, "description": f"Unauthorized {TOKEN}"}))
    with pytest.raises(NotifyError) as exc:
        api.get_me()
    assert TOKEN not in str(exc.value) and "***" in str(exc.value)
    assert TOKEN not in repr(api)


def test_api_sends_documents(tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    http = RecordingHttp({"ok": True, "result": {}})
    TelegramApi(TOKEN, http).send_document("42", pdf, "caption")
    url, payload = http.calls[0]
    assert url.endswith("/sendDocument") and b"cv.pdf" in payload["raw"]
