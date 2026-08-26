"""WhatsApp gateway adapter: how the gateway's responses are interpreted.

Every test here exists because of a bug found against the live gateway. The theme is the
same each time: the vendored Go service marshals structs whose fields carry no json tags,
so they arrive with Go's exported names ("Connected", "LoggedIn", "PairingCode") rather
than the lowercase keys an HTTP API usually uses. Reading the wrong key does not raise -
it silently yields False or None, which is the worst possible failure for a health signal.

The distinction that matters most is Connected vs LoggedIn. `Connected` is only the
websocket to WhatsApp, and it is true the whole time a pairing code sits unentered. Only
`LoggedIn` means a number is actually linked and a send can succeed. Reporting `Connected`
as "connected" shows a healthy gateway in the console while every alert fails to send.

No network: an httpx MockTransport stands in for the gateway.
"""
from __future__ import annotations

import json

import httpx

from csense_shared.notifications.whatsapp import EvolutionGoWhatsAppProvider

TOKEN = "instance-token-for-tests"


def provider_for(handler) -> EvolutionGoWhatsAppProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvolutionGoWhatsAppProvider(
        base_url="http://whatsapp-gateway:8080",
        admin_api_key="admin-key",
        instance_token=TOKEN,
        instance_name="csense",
        client=client,
    )


def status_returning(data: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instance/status"
        # Instance-scoped routes must use the instance token, never the admin key.
        assert request.headers["apikey"] == TOKEN
        return httpx.Response(200, json={"message": "success", "data": data})

    return handler


# --- Connected vs LoggedIn -----------------------------------------------------------

async def test_websocket_up_but_no_number_linked_is_not_connected():
    """The exact live response seen while a pairing code was outstanding.

    Connected:true here means only that the gateway reached WhatsApp's servers. Treating
    it as usable is how a gateway that can send nothing reports itself healthy.
    """
    provider = provider_for(
        status_returning({"Connected": True, "LoggedIn": False, "Name": ""})
    )
    status = await provider.instance_status()

    assert status["connected"] is False
    assert status["state"] == "awaiting_pairing"
    assert "no number is linked" in status["detail"]


async def test_logged_in_is_connected():
    provider = provider_for(
        status_returning({"Connected": True, "LoggedIn": True, "Name": "CSense Alerts"})
    )
    status = await provider.instance_status()

    assert status["connected"] is True
    assert status["state"] == "connected"
    assert status["account_name"] == "CSense Alerts"


async def test_socket_down_is_disconnected():
    provider = provider_for(status_returning({"Connected": False, "LoggedIn": False}))
    status = await provider.instance_status()

    assert status["connected"] is False
    assert status["state"] == "disconnected"


async def test_lowercase_keys_still_understood():
    """Guards the fallback: a future gateway version may add json tags."""
    provider = provider_for(status_returning({"connected": True, "loggedIn": True}))
    status = await provider.instance_status()

    assert status["connected"] is True


# --- Pairing -------------------------------------------------------------------------

async def test_pairing_code_read_from_go_exported_field():
    """PairReturnStruct.PairingCode has no json tag, so it arrives capitalised."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instance/pair"
        # whatsmeow's PairPhone takes the number without a leading '+'.
        assert json.loads(request.read()) == {"phone": "918110080350"}
        return httpx.Response(
            200, json={"message": "success", "data": {"PairingCode": "PF8RF7TF"}}
        )

    result = await provider_for(handler).pair_phone("+918110080350")

    assert result["ok"] is True
    assert result["pairing_code"] == "PF8RF7TF"


async def test_pairing_rejects_non_e164_without_calling_the_gateway():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the gateway")

    result = await provider_for(handler).pair_phone("8110080350")

    assert result["ok"] is False
    assert "E.164" in result["detail"]


async def test_pairing_an_already_linked_instance_reports_the_reason():
    """The gateway 500s with 'already authenticated'; that must not read as a transport
    fault, or the console shows an outage where the truth is 'nothing to do'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "instance is already authenticated"})

    result = await provider_for(handler).pair_phone("+918110080350")

    assert result["ok"] is False
    assert "already authenticated" in result["detail"]


# --- Redaction -----------------------------------------------------------------------

async def test_pairing_failure_detail_is_redacted():
    """Gateway errors echo the number. It must not land in a stored detail string."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="pairing failed for +918110080350")

    result = await provider_for(handler).pair_phone("+918110080350")

    assert "918110080350" not in result["detail"]
