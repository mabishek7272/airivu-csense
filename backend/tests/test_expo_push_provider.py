"""ExpoPushProvider: sends via Expo's hosted push relay (no APNs cert / FCM project
needed - Expo's push service is the relay for both). Uses a fake HTTP client so this
test never makes a real network call.
"""
from __future__ import annotations

import pytest

from csense_shared.notifications.expo_push import ExpoPushProvider
from csense_shared.notifications.providers import DeliveryOutcome, Message


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict) -> None:
        self.status_code = status_code
        self._json = json_body

    def json(self) -> dict:
        return self._json


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_request: dict | None = None

    async def post(self, url: str, *, json: dict) -> _FakeResponse:
        self.last_request = {"url": url, "json": json}
        return self._response


@pytest.mark.asyncio
async def test_a_successful_send_reports_accepted():
    http = _FakeHttpClient(_FakeResponse(200, {"data": {"status": "ok"}}))
    provider = ExpoPushProvider(client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[abc123]", subject="Alert", body="Something happened"))

    assert result.accepted is True
    assert http.last_request is not None
    assert http.last_request["json"]["to"] == "ExponentPushToken[abc123]"
    assert http.last_request["json"]["body"] == "Something happened"


@pytest.mark.asyncio
async def test_a_device_not_registered_error_is_reported_as_not_accepted():
    http = _FakeHttpClient(
        _FakeResponse(200, {"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})
    )
    provider = ExpoPushProvider(client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[stale]", subject="Alert", body="Something happened"))

    assert result.accepted is False
    assert result.outcome is DeliveryOutcome.FAILED_PERMANENT
    assert result.failure_code == "device_not_registered"


@pytest.mark.asyncio
async def test_an_http_error_is_reported_as_not_accepted():
    http = _FakeHttpClient(_FakeResponse(500, {}))
    provider = ExpoPushProvider(client=http)

    result = await provider.send(Message(recipient="ExponentPushToken[abc]", subject="Alert", body="body"))

    assert result.accepted is False
    assert result.outcome is DeliveryOutcome.FAILED_RETRYABLE
