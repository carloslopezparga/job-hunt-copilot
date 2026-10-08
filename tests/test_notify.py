from __future__ import annotations

import json

import pytest

from jobhunt.models import Job
from jobhunt.notify import NotifyError, TelegramNotifier, notify_offers, offer_message
from jobhunt.tracker import Tracker

TOKEN = "123456:ABC-secret-token"


class FakeHttp:
    """Records requests; optionally fails for some methods."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []
        self.fail_on = fail_on

    def post(self, url, body, headers):
        self.calls.append((url, body, dict(headers)))
        if self.fail_on and url.endswith(self.fail_on):
            return {"ok": False, "description": f"Bad Request: chat not found ({url})"}
        return {"ok": True, "result": {}}


def notifier(http):
    return TelegramNotifier(token=TOKEN, chat_id="42", http=http, delay=0)


@pytest.fixture
def records(tmp_path):
    with Tracker(tmp_path / "t.db") as t:
        t.upsert_jobs([
            Job("lever", "1", "Paylane", "paylane", "Data Intern", "https://x/1",
                location="Madrid, Spain", description="Python and SQL"),
            Job("lever", "2", "Helix", "helix", "ML Intern", "https://x/2",
                location="Remote - EU"),
        ])  # fmt: skip
        t.set_score(1, 91, "internship ✓ · Madrid, Spain (preferred)")
        t.set_score(2, 80, "internship ✓ · Remote - EU (acceptable)")
        yield t.jobs()


def test_from_env_requires_both_variables():
    assert TelegramNotifier.from_env({}) is None
    assert TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
    assert TelegramNotifier.from_env({"TELEGRAM_CHAT_ID": "42"}) is None
    n = TelegramNotifier.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": " 42 "})
    assert n is not None and n.chat_id == "42"
    assert TOKEN not in repr(n)  # token never shows up in logs/reprs


def test_offer_message_has_required_fields(records):
    text = offer_message(records[0])
    for part in ("91/100", "Paylane", "Data Intern", "Madrid, Spain", "preferred", "https://x/1"):
        assert part in text


def test_send_message_request():
    http = FakeHttp()
    notifier(http).send_message("hola")
    url, body, headers = http.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"chat_id": "42", "text": "hola", "disable_web_page_preview": True}


def test_send_document_multipart(tmp_path):
    pdf = tmp_path / "cv_en.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    http = FakeHttp()
    notifier(http).send_document(pdf, caption="Paylane — Data Intern")
    url, body, headers = http.calls[0]
    assert url.endswith("/sendDocument")
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"\r\n\r\n42' in body
    assert b'filename="cv_en.pdf"' in body and b"%PDF-1.4 fake" in body
    assert "Paylane — Data Intern".encode() in body


def test_errors_never_leak_the_token():
    with pytest.raises(NotifyError) as info:
        notifier(FakeHttp(fail_on="sendMessage")).send_message("x")
    assert TOKEN not in str(info.value) and "***" in str(info.value)


def test_notify_offers_with_attachments_and_partial_failure(records, tmp_path):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"pdf")
    letter = tmp_path / "letter.txt"
    letter.write_text("Dear team")
    http = FakeHttp()
    errors = notify_offers(notifier(http), records, {records[0].id: [cv, letter]})
    assert errors == []
    assert [u.rsplit("/", 1)[1] for u, _, _ in http.calls] == [
        "sendMessage", "sendDocument", "sendDocument", "sendMessage",
    ]  # fmt: skip

    failing = FakeHttp(fail_on="sendDocument")
    errors = notify_offers(notifier(failing), records, {records[0].id: [cv]})
    assert len(errors) == 1 and errors[0].startswith(f"#{records[0].id}")
    assert sum(u.endswith("sendMessage") for u, _, _ in failing.calls) == 2  # kept going
