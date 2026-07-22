"""Tests for the external sandbox runtime controller adapter."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request

import pytest

from omnigent.onboarding.sandboxes.remote import RemoteSandboxLauncher


class _Response:
    def __init__(self, body: dict[str, object] | None = None) -> None:
        self._body = json.dumps(body).encode() if body is not None else b""

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_provision_sends_launch_context_and_returns_stable_runtime_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        assert timeout == 15 * 60
        requests.append(request)
        return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setenv("PLATFORM_GIT_BROKER_URL", "https://platform.example.com/git")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(
        url="https://platform.example.com",
        env=["PLATFORM_GIT_BROKER_URL"],
    )
    launcher.set_launch_context(owner="alice@example.com", session_id="conv_alice")

    assert launcher.provision("managed-abcd1234") == "runtime_abc"
    payload = json.loads(requests[0].data or b"{}")
    assert payload == {
        "name": "managed-abcd1234",
        "owner": "alice@example.com",
        "sessionId": "conv_alice",
        "env": {"PLATFORM_GIT_BROKER_URL": "https://platform.example.com/git"},
    }
    assert requests[0].headers["Authorization"] == "Bearer runtime-secret"
    assert requests[0].headers["X-sandbox-runtime-api-version"] == "1"


def test_run_uses_controller_command_endpoint_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        seen["url"] = request.full_url
        seen["payload"] = json.loads(request.data or b"{}")
        return _Response({"result": {"exitCode": 0, "stdout": "hello\n", "stderr": ""}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    result = launcher.run("runtime_abc", "printf hello")

    assert seen["url"].endswith("/api/v1/sandbox-runtimes/runtime_abc/commands")
    assert seen["payload"] == {"command": "printf hello", "timeoutSeconds": 15 * 60}
    assert result.returncode == 0
    assert result.stdout == "hello\n"


def test_stopped_runtime_is_resumed_through_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def _urlopen(request: Request, *, timeout: int) -> _Response:
        del timeout
        requests.append((request.method, request.full_url))
        if request.method == "GET":
            return _Response({"runtime": {"id": "runtime_abc", "state": "stopped"}})
        return _Response({"runtime": {"id": "runtime_abc", "state": "running"}})

    monkeypatch.setenv("OMNIGENT_REMOTE_SANDBOX_TOKEN", "runtime-secret")
    monkeypatch.setattr("omnigent.onboarding.sandboxes.remote.urlopen", _urlopen)
    launcher = RemoteSandboxLauncher(url="https://platform.example.com")

    assert launcher.is_running("runtime_abc") is False
    assert launcher.exists("runtime_abc") is True
    launcher.resume("runtime_abc")

    assert requests[-1] == (
        "POST",
        "https://platform.example.com/api/v1/sandbox-runtimes/runtime_abc/resume",
    )
