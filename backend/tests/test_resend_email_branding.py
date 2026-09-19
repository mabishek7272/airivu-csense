"""White-label branding fields on ResendEmailProvider - pure logic, no network calls
(a real send is verified separately, live, against the real Resend API - see this
session's own notes; these tests pin the composition logic itself)."""
from __future__ import annotations

import sys
from pathlib import Path

SHARED_ROOT = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from csense_shared.notifications.providers import Message
from csense_shared.notifications.resend_email import ResendEmailProvider


def _provider(from_address: str = "no-reply@3rdi.in") -> ResendEmailProvider:
    return ResendEmailProvider(api_key="test-key", from_address=from_address)


def test_from_header_uses_configured_address_when_no_brand_name():
    provider = _provider()
    message = Message(recipient="x@example.com", subject="s", body="b")
    assert provider._from_header(message) == "no-reply@3rdi.in"


def test_from_header_composes_brand_name_with_the_shared_address():
    """The sending *address* never changes per brand (shared Resend domain, no new
    SPF/DKIM) - only the display name does."""
    provider = _provider()
    message = Message(recipient="x@example.com", subject="s", body="b", from_name="eAIgleye Alerts")
    assert provider._from_header(message) == "eAIgleye Alerts <no-reply@3rdi.in>"


def test_from_header_extracts_bare_address_even_if_configured_with_a_display_name():
    provider = _provider(from_address="CSense Alerts <no-reply@3rdi.in>")
    message = Message(recipient="x@example.com", subject="s", body="b", from_name="Apti SafeGuard")
    assert provider._from_header(message) == "Apti SafeGuard <no-reply@3rdi.in>"


def test_default_footer_is_unchanged_for_an_unbranded_message():
    provider = _provider()
    message = Message(recipient="x@example.com", subject="s", body="hello")
    html = provider._render_html(message)
    assert "Sent by AIRIVU CSense" in html
    assert "<img" not in html  # no brand logo for the default case


def test_branded_footer_and_logo_replace_the_default():
    provider = _provider()
    message = Message(
        recipient="x@example.com", subject="s", body="hello",
        brand_logo_url="https://example.invalid/logo.png",
        brand_footer_text="Sent by eAIgleye.",
    )
    html = provider._render_html(message)
    assert "Sent by eAIgleye." in html
    assert "Sent by AIRIVU CSense" not in html
    assert 'src="https://example.invalid/logo.png"' in html


def test_branded_html_escapes_the_logo_url_and_footer_text():
    """Both are admin-supplied (org_branding), not attacker-supplied per se, but this
    still goes through the same escaping discipline as the body - a stored XSS via a
    branding field would otherwise land in every alert email a tenant's whole team
    reads."""
    provider = _provider()
    message = Message(
        recipient="x@example.com", subject="s", body="hello",
        brand_logo_url='https://example.invalid/a"b.png',
        brand_footer_text="<script>evil()</script>",
    )
    html = provider._render_html(message)
    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;" in html
