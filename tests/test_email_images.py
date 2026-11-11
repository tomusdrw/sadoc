"""Logos must travel with the email, without depending on public image hosting."""

import base64
import io
import json
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from sadoc import alerts, notify


@pytest.fixture
def smtp_messages(monkeypatch):
    messages = []

    @contextmanager
    def smtp(host, port, timeout):
        yield SimpleNamespace(send_message=messages.append)

    monkeypatch.setattr(notify.smtplib, "SMTP", smtp)
    return messages


def test_smtp_alert_contains_a_related_logo_matching_the_html(smtp_messages):
    backend = notify.SmtpBackend("smtp.example.com", use_tls=False)
    notifier = notify.Notifier(backend, to=["reader@example.com"])
    alert = alerts.unsupported_ecosystem("owner/repo", ["Gradle"], ["build.gradle"])

    assert notifier.send(alert) == "sent"

    message = BytesParser(policy=policy.default).parsebytes(smtp_messages[0].as_bytes())
    html_part = message.get_body(preferencelist=("html",))
    images = [part for part in message.walk() if part.get_content_maintype() == "image"]
    assert len(images) == 1
    image = images[0]
    cid = str(image["Content-ID"]).strip("<>")
    assert f'src="cid:{cid}"' in html_part.get_content()
    assert image.get_content_disposition() == "inline"
    assert image.get_content_type() == "image/png"
    assert image.get_payload(decode=True).startswith(b"\x89PNG\r\n\x1a\n")
    assert html_part in list(message.get_payload()[1].iter_parts())
    assert message.get_payload()[1].get_content_type() == "multipart/related"
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == alert.text


def test_resend_alert_embeds_base64_logo_with_a_matching_content_id(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"id":"sent-logo"}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    notifier = notify.Notifier(
        notify.ResendBackend("test-key", "sender@example.com"), to=["reader@example.com"]
    )
    alert = alerts.unsupported_ecosystem("owner/repo", ["Gradle"], ["build.gradle"])

    assert notifier.send(alert) == "sent"

    payload = json.loads(requests[0].data)
    assert len(payload["attachments"]) == 1
    image = payload["attachments"][0]
    assert f"cid:{image['content_id']}" in payload["html"]
    assert image["content_type"] == "image/png"
    assert base64.b64decode(image["content"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert "path" not in image  # No public URL or local path sent to the provider.
    assert payload["text"] == alert.text


def test_plain_text_smtp_messages_do_not_get_an_image(smtp_messages):
    backend = notify.SmtpBackend("smtp.example.com", use_tls=False)
    backend.send(notify.Message(subject="News", text="A plain note."))

    assert not smtp_messages[0].is_multipart()
    assert smtp_messages[0].get_content().strip() == "A plain note."


def test_plain_text_resend_messages_do_not_get_an_image(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"id":"sent-text"}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    backend = notify.ResendBackend("test-key", "sender@example.com")
    backend.send(notify.Message(subject="News", text="A plain note."))

    assert "attachments" not in json.loads(requests[0].data)
