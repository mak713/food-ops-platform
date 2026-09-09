"""A minimal injectable mail interface for the password-reset flow.

Real SMTP delivery is deferred to deployment (Spec §10.6 explicitly permits a dev/test
sink before then). Neither implementation here routes through `configure_logging()`'s
application logger — Spec §11.10 requires reset tokens/links never be logged, so the
outbox is a dedicated, separate channel rather than an intentional-but-sensitive log line.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol


class Mailer(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


class OutboxFileMailer:
    """Development mailer: writes each message to a local, gitignored outbox directory so a
    developer running the app can open and read it (e.g. a password-reset link) without
    that content ever touching the structured application log stream."""

    def __init__(self, outbox_dir: Path | None = None) -> None:
        # backend/app/core/mail.py -> parents[2] == backend/
        self._outbox_dir = outbox_dir or (Path(__file__).resolve().parents[2] / ".mail-outbox")

    def send(self, *, to: str, subject: str, body: str) -> None:
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
        safe_to = to.replace("/", "_").replace("\\", "_")
        path = self._outbox_dir / f"{timestamp}-{safe_to}.txt"
        path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")


class InMemoryMailer:
    """Test double: captures sent messages in-process so tests can assert on them directly,
    without a filesystem side effect or any log inspection."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


def get_mailer() -> Mailer:
    """FastAPI dependency provider. Tests override this with an `InMemoryMailer` instance
    via `app.dependency_overrides[get_mailer]`."""
    return OutboxFileMailer()
