"""The NVR adapter interface and its one mock reference adapter.

Not a test of real NVR connectivity - `MockNVRAdapter` never opens a socket, by design
(see nvr_adapter.py's own docstring for why this pass scopes exactly one, mock, adapter).
What's worth pinning: the shape callers get back, that it never touches the network no
matter what it's asked to connect to, and that the registry a real adapter will plug into
later actually works.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_nvr_adapter():
    """Loads the module by path, not `sys.path` + `from app...`.

    Every service names its package `app` (tenant_api, admin_api, ai_runtime,
    notification_worker) - a plain import would resolve to whichever service's `app`
    happened to be cached first by another test module collected earlier in the same run,
    silently exercising the wrong code. Same fix `test_notification_worker.py` already
    uses for the identical problem.

    Registered into `sys.modules` before `exec_module` runs - `nvr_adapter.py` uses
    `@dataclass` under `from __future__ import annotations`, and `dataclasses` resolves
    forward-referenced type hints via `sys.modules[cls.__module__]`; skip the registration
    (as a first pass here did) and that lookup returns `None` and crashes on import.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "tenant_api" / "app" / "services" / "nvr_adapter.py"
    )
    spec = importlib.util.spec_from_file_location("csense_tenant_nvr_adapter", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_nvr_adapter = _load_nvr_adapter()
ADAPTERS_BY_KIND = _nvr_adapter.ADAPTERS_BY_KIND
MockNVRAdapter = _nvr_adapter.MockNVRAdapter
NVRChannel = _nvr_adapter.NVRChannel
build_adapter = _nvr_adapter.build_adapter


async def test_mock_adapter_returns_a_deterministic_nonempty_channel_list():
    adapter = MockNVRAdapter()
    channels = await adapter.list_channels(
        hostname="192.0.2.1", port=80, username="u", password="p"
    )

    assert len(channels) > 0
    assert all(isinstance(c, NVRChannel) for c in channels)
    # Every channel needs both stream paths and an id to be turned into a real CameraIn.
    for channel in channels:
        assert channel.channel_id
        assert channel.main_stream_path
        assert channel.name


async def test_mock_adapter_result_does_not_depend_on_what_it_was_asked_to_connect_to():
    adapter = MockNVRAdapter()
    a = await adapter.list_channels(hostname="10.0.0.1", port=1, username="a", password="a")
    b = await adapter.list_channels(hostname="203.0.113.9", port=8899, username="x", password="y")
    assert a == b


async def test_mock_adapter_never_touches_a_real_socket(monkeypatch):
    """The whole point of shipping exactly one *mock* adapter this pass - confirmed, not
    just asserted in a docstring."""
    import socket

    def _refuse(*args, **kwargs):
        raise AssertionError("MockNVRAdapter must never open a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    adapter = MockNVRAdapter()
    channels = await adapter.list_channels(
        hostname="example.invalid", port=554, username="u", password="p"
    )
    assert len(channels) > 0


def test_registry_resolves_the_mock_adapter_by_kind():
    assert ADAPTERS_BY_KIND["mock"] is MockNVRAdapter
    assert isinstance(build_adapter("mock"), MockNVRAdapter)


def test_registry_refuses_an_unknown_kind():
    with pytest.raises(ValueError, match="onvif"):
        build_adapter("onvif")
