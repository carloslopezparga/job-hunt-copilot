"""Telegram notifications for `jobhunt run --notify telegram`.

Credentials come ONLY from the environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID),
never from the repository or profile.yaml. Notifications go to the user's own chat;
nothing is ever sent to employers or recruiters.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jobhunt.tracker import JobRecord

API_BASE = "https://api.telegram.org"
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024


class NotifyError(RuntimeError):
    pass


class HttpPoster(Protocol):
    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> dict:
        """POST ``body`` and return the decoded JSON response."""


class UrllibPoster:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def post(self, url: str, body: bytes, headers: Mapping[str, str]) -> dict:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                return {"ok": False, "description": f"HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return {"ok": False, "description": str(exc.__class__.__name__)}


def _multipart(fields: Mapping[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    boundary = f"jobhunt-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{path.name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
        + path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass
class TelegramNotifier:
    token: str = field(repr=False)
    chat_id: str
    http: HttpPoster = field(default_factory=UrllibPoster)
    delay: float = 0.4  # stay well below Telegram's rate limits

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, http: HttpPoster | None = None
    ) -> TelegramNotifier | None:
        """Build from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, or None if either is missing."""
        env = os.environ if env is None else env
        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            return None
        return cls(token=token, chat_id=chat_id, http=http or UrllibPoster())

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def _check(self, response: dict, what: str) -> None:
        if not response.get("ok"):
            # Never echo the URL: it contains the bot token.
            reason = str(response.get("description", "unknown error")).replace(self.token, "***")
            raise NotifyError(f"Telegram {what} failed: {reason}")
        if self.delay:
            time.sleep(self.delay)

    def send_message(self, text: str) -> None:
        body = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text[:MESSAGE_LIMIT],
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        response = self.http.post(
            self._url("sendMessage"), body, {"Content-Type": "application/json"}
        )
        self._check(response, "sendMessage")

    def send_document(self, path: str | Path, caption: str = "") -> None:
        body, ctype = _multipart(
            {"chat_id": self.chat_id, "caption": caption[:CAPTION_LIMIT]}, "document", Path(path)
        )
        response = self.http.post(self._url("sendDocument"), body, {"Content-Type": ctype})
        self._check(response, f"sendDocument({Path(path).name})")


def offer_message(record: JobRecord) -> str:
    """Short plain-text message: company, title, location, score, reason, link."""
    job = record.job
    lines = [
        f"{record.score}/100 · {job.company}",
        job.title,
        f"📍 {job.location or job.workplace_type}",
        f"Why: {record.score_explanation or '-'}",
        job.url,
        f"(tracker id {record.id})",
    ]
    return "\n".join(lines)


def notify_offers(
    notifier: TelegramNotifier,
    records: list[JobRecord],
    attachments: Mapping[int, list[Path]] | None = None,
) -> list[str]:
    """Send one message per offer (plus its CV/letter if given). Returns error strings;
    a failure on one offer never stops the others."""
    attachments = attachments or {}
    errors: list[str] = []
    for record in records:
        try:
            notifier.send_message(offer_message(record))
            for path in attachments.get(record.id, []):
                notifier.send_document(path, caption=f"{record.job.company} — {record.job.title}")
        except (NotifyError, OSError) as exc:
            errors.append(f"#{record.id}: {exc}")
    return errors


__all__ = [
    "HttpPoster",
    "NotifyError",
    "TelegramNotifier",
    "UrllibPoster",
    "notify_offers",
    "offer_message",
]
