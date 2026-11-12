"""Email notifications.

GitHub deliberately does not email you about activity on your own pull requests,
so the bot has to tell you directly. Delivery is pluggable:

  resend   - HTTP API (one POST, no dependencies)
  smtp     - direct SMTP via the standard library (no MTA needed)
  console  - print to stdout, for dry runs
  none     - discard

Secrets are never stored in the config file: the config names an *environment
variable* and the value is read from the environment at send time. The agent
sandbox scrubs those variables, so the model can never read the API key.
"""

from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from importlib.resources import files

from .email_template import LOGO_CID


class NotifyError(RuntimeError):
    pass


@dataclass
class InlineImage:
    filename: str
    content: bytes
    content_id: str
    content_type: str = "image/png"


@dataclass
class Message:
    subject: str
    text: str
    html: str | None = None
    to: list[str] = field(default_factory=list)
    from_addr: str | None = None
    headers: dict = field(default_factory=dict)
    inline_images: list[InlineImage] = field(default_factory=list)

    def body(self) -> str:
        if self.html:
            return self.html
        return f"<pre>{_escape(self.text)}</pre>"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Backend:
    name = "none"

    def send(self, message: Message) -> str:
        raise NotImplementedError


class NullBackend(Backend):
    name = "none"

    def send(self, message: Message) -> str:
        return "null"


class ConsoleBackend(Backend):
    """Print the message instead of sending it. Used for --dry-run."""

    name = "console"

    def send(self, message: Message) -> str:
        print(f"--- EMAIL TO {', '.join(message.to)} ---")
        print(f"Subject: {message.subject}")
        print(message.text)
        print("--- end email ---")
        return "console"


class ResendBackend(Backend):
    name = "resend"
    endpoint = "https://api.resend.com/emails"

    def __init__(self, api_key: str, from_addr: str):
        if not api_key:
            raise NotifyError("Resend API key is empty")
        self.api_key = api_key
        self.from_addr = from_addr

    def send(self, message: Message) -> str:
        payload = {
            "from": message.from_addr or self.from_addr,
            "to": message.to or [],
            "subject": message.subject,
            "html": message.body(),
            "text": message.text,
        }
        if message.headers:
            payload["headers"] = message.headers
        if message.html and message.inline_images:
            payload["attachments"] = [
                {
                    "filename": image.filename,
                    "content": base64.b64encode(image.content).decode("ascii"),
                    "content_id": image.content_id,
                    "content_type": image.content_type,
                }
                for image in message.inline_images
            ]
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "sadoc/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise NotifyError(f"Resend HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotifyError(f"Resend unreachable: {exc}") from exc
        try:
            return json.loads(body).get("id", "unknown")
        except json.JSONDecodeError:
            return "unknown"


class SmtpBackend(Backend):
    """Direct SMTP submission (port 587 + STARTTLS).

    No local MTA is required - smtplib talks to the provider directly.
    """

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int = 587,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
        use_tls: bool = True,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.use_tls = use_tls

    def send(self, message: Message) -> str:
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = message.from_addr or self.from_addr or self.user or "sadoc"
        msg["To"] = ", ".join(message.to)
        for key, value in (message.headers or {}).items():
            msg[key] = value
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
            html_part = msg.get_payload()[-1]
            for image in message.inline_images:
                maintype, subtype = image.content_type.split("/", 1)
                html_part.add_related(
                    image.content,
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{image.content_id}>",
                    filename=image.filename,
                    disposition="inline",
                )

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                if self.use_tls:
                    server.starttls(context=ssl.create_default_context())
                if self.user and self.password:
                    server.login(self.user, self.password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP failed: {exc}") from exc
        return "sent"


def build_backend(cfg_notify: dict) -> Backend:
    """Create the configured backend. Reads secrets from the environment."""
    backend = (cfg_notify.get("backend") or "none").lower()
    from_addr = cfg_notify.get("from")

    if backend == "none":
        return NullBackend()
    if backend == "console":
        return ConsoleBackend()
    if backend == "resend":
        key_env = cfg_notify.get("resend_api_key_env") or "RESEND_API_KEY"
        key = os.environ.get(key_env, "")
        if not key:
            raise NotifyError(
                f"notify backend is 'resend' but ${key_env} is not set in the environment"
            )
        return ResendBackend(key, from_addr or "sadoc@localhost")
    if backend == "smtp":
        pw_env = cfg_notify.get("smtp_password_env") or "SMTP_PASSWORD"
        return SmtpBackend(
            host=cfg_notify.get("smtp_host") or "smtp.gmail.com",
            port=int(cfg_notify.get("smtp_port") or 587),
            user=cfg_notify.get("smtp_user"),
            password=os.environ.get(pw_env, ""),
            from_addr=from_addr,
            use_tls=bool(cfg_notify.get("smtp_starttls", True)),
        )
    raise NotifyError(f"unknown notify backend '{backend}'")


class Notifier:
    """Sends alerts, with dedupe so the same thing is not sent twice."""

    def __init__(
        self,
        backend: Backend,
        state=None,
        dry_run: bool = False,
        to: list[str] | None = None,
        from_addr: str | None = None,
    ):
        self.backend = backend
        self.state = state
        self.dry_run = dry_run
        self.to = to or []
        self.from_addr = from_addr

    def send(self, alert, force: bool = False) -> str:
        """Send an alert. Returns 'sent', 'duplicate', 'dry-run' or 'failed'."""
        if (
            self.state is not None
            and alert.dedupe_key
            and not force
            and self.state.notification_sent(alert.dedupe_key)
        ):
            return "duplicate"
        if self.dry_run:
            # A test run must not send mail. The alert is still recorded, so a
            # later real run can still send it.
            if self.state is not None and alert.dedupe_key:
                self.state.record_notification(
                    alert.dedupe_key,
                    alert.repo,
                    alert.pr_number,
                    alert.kind,
                    alert.subject,
                    "dry-run",
                    ok=False,
                )
            return "dry-run"
        html_body = getattr(alert, "html", None)
        inline_images = []
        if html_body and f"cid:{LOGO_CID}" in html_body:
            inline_images.append(
                InlineImage(
                    filename="sadoc-logo.png",
                    content=files("sadoc").joinpath("logo.png").read_bytes(),
                    content_id=LOGO_CID,
                )
            )
        message = Message(
            subject=alert.subject,
            text=alert.text,
            html=html_body,
            to=self.to,
            from_addr=self.from_addr,
            headers=getattr(alert, "headers", {}) or {},
            inline_images=inline_images,
        )
        try:
            sent_id = self.backend.send(message)
            status = "sent"
        except NotifyError as exc:
            sent_id = str(exc)
            status = "failed"
        if self.state is not None and alert.dedupe_key:
            self.state.record_notification(
                alert.dedupe_key,
                alert.repo,
                alert.pr_number,
                alert.kind,
                alert.subject,
                sent_id,
                ok=(status == "sent"),
            )
        return status
