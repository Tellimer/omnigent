"""
Daytona sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`Daytona <https://www.daytona.io>`_ sandboxes. This module ships in the
OSS build; the Daytona SDK itself is an optional dependency
(``pip install 'omnigent[daytona]'``) imported lazily, so the provider
can be listed and the module probed without it.

Supports both server-managed hosts (``host_type="managed"`` sessions —
``prepare`` / ``provision`` / ``run`` / ``terminate``) and the CLI
bootstrap flow (``omnigent sandbox create`` / ``connect`` — file
shipping via the SDK's filesystem API, foreground attach via a PTY
session). The one unimplemented primitive is ``stream_exec``: its only
consumer is the in-sandbox App OAuth login, which requires
local-to-sandbox port forwarding that Daytona doesn't have — the flow
fails fast on :attr:`SandboxLauncher.supports_local_port_forward`
before ``stream_exec`` would ever run.

Platform notes that shape this launcher:

- **No hard lifetime cap, with resumable idle auto-stop.** Daytona stops
  sandboxes after 15 idle minutes BY DEFAULT. Managed sessions instead use
  a one-hour idle window: stopped sandboxes retain their filesystem, and the
  next Omnigent message starts the same sandbox and host again. This releases
  idle compute without turning an old session into a dead end.
- **Workload env rides sandbox creation.** Daytona has no named-secret
  store to attach at create time; harness credentials are injected as
  literal ``env_vars``, resolved BY NAME from the server process
  environment (``sandbox.daytona.env`` config /
  :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`) so secret values never live
  in the server config file.
- **No inbound port forwarding.** Daytona preview links expose sandbox
  ports publicly but provide no local→sandbox path, so
  ``supports_local_port_forward`` stays ``False``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click

from omnigent.inner import ui
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from pathlib import Path

    import daytona as daytona_sdk
    from daytona._sync.sandbox import Sandbox as DaytonaSandbox
    from daytona.handle.pty_handle import PtyHandle


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_DAYTONA_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for
Daytona sandboxes, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_DAYTONA_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process
environment variables whose values are injected into every sandbox this
launcher creates — typically the harness LLM credentials
(``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, gateway base URLs, …) and
``GIT_TOKEN`` that the in-sandbox host forwards to runners. Names, not
values: the values are read from the server's own environment at
provision time, so secrets never live in config files. The server's
managed-host config (``sandbox.daytona.env``) takes precedence when
set."""

# Resources for the sandbox. Matches the Modal launcher's sizing: 2
# vCPU / 4 GiB is enough for a host running one interactive session
# (Daytona's Resources units are vCPUs and GiB).
_SANDBOX_CPU: int = 2
_SANDBOX_MEMORY_GIB: int = 4

# Sandbox-creation timeout. The first create from a given image makes
# Daytona pull the image and build an internal snapshot, which for the
# ~1.4 GiB host image takes minutes; later creates reuse the snapshot
# and take seconds. The SDK default (60 s) only covers the warm path.
_CREATE_TIMEOUT_S: float = 900.0

# Daytona's idle auto-stop is expressed in minutes. A one-hour window releases
# compute promptly while preserving the sandbox filesystem. ``resume`` starts
# the SAME sandbox on the next message.
_MANAGED_AUTO_STOP_MINUTES: int = 60

# CLI-attached sandboxes still use keep_alive()'s historical contract: the
# foreground host lives until the operator disconnects or deletes it.
_AUTO_STOP_DISABLED: int = 0

# Terminate retries when Daytona reports a state-change conflict (e.g.
# a deletion another cleanup path already started). 3 attempts × 2 s
# covers the observed settle time without stalling best-effort
# teardown callers.
_TERMINATE_CONFLICT_RETRIES: int = 3
_TERMINATE_CONFLICT_BACKOFF_S: float = 2.0


def _ensure_sdk() -> None:
    """
    Verify the Daytona SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is
    an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``daytona`` package is not
        installed.
    """
    try:
        import daytona  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The Daytona SDK is required for the 'daytona' sandbox "
            "provider. Install it with `pip install 'omnigent[daytona]'`, "
            "then set DAYTONA_API_KEY (create a key at "
            "https://app.daytona.io)."
        ) from exc


def _drive_foreground_pty(pty: PtyHandle, sandbox_id: str, command: str) -> int:
    """
    Drive a freshly-created PTY session through one foreground command.

    Sends the command (``TERM`` forced for tmux-spawning harnesses,
    ``exec`` so the PTY's close frame carries the command's own exit
    code), echoes output to the local terminal until exit, and tears
    the websocket down.

    :param pty: Handle for a just-created PTY session (already
        connected; the SDK waits for the connection during creation).
    :param sandbox_id: Sandbox the session runs in, for error messages.
    :param command: Shell command to execute remotely, e.g.
        ``"omnigent host --server https://…"``.
    :returns: The remote command's exit code.
    :raises click.ClickException: When the session ends without
        reporting an exit code (e.g. a dropped websocket).
    :raises KeyboardInterrupt: Re-raised after killing the remote
        process when the user detaches with Ctrl-C.
    """
    try:
        pty.send_input(f"TERM=xterm-256color exec {command}\n")
        result = pty.wait(
            on_data=lambda data: click.echo(data.decode("utf-8", errors="replace"), nl=False)
        )
    except KeyboardInterrupt:
        click.echo("\n  → detaching; stopping the remote process")
        pty.kill()
        raise
    finally:
        pty.disconnect()
    if result.exit_code is None:
        # The websocket dropped (or the daemon reported an error)
        # before the close frame carried an exit code — fail loud
        # rather than inventing a status.
        raise click.ClickException(
            f"The PTY session on sandbox '{sandbox_id}' ended without "
            f"an exit code{f': {result.error}' if result.error else ''}."
        )
    return result.exit_code


class DaytonaSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Daytona sandboxes.

    All transport rides the Daytona SDK: ``sandbox.process.exec`` for
    commands (the Daytona toolbox runs them through a shell, with the
    two output streams merged into one result), ``sandbox.fs`` for
    file shipping, PTY sessions for the foreground attach, and
    ``Daytona.create`` / ``delete`` for lifecycle. Handles are cached
    per sandbox id to avoid a server round-trip on every primitive.
    """

    provider: ClassVar[str] = "daytona"
    # Daytona preview links are sandbox→public only; there is no
    # local→sandbox path for the App OAuth callback port.
    supports_local_port_forward: ClassVar[bool] = False
    # Daytona stop/start preserves the sandbox filesystem. The managed-session
    # wake path can therefore revive a cold session in place.
    can_resume: ClassVar[bool] = True

    def __init__(self, *, image: str | None = None, env: Sequence[str] | None = None) -> None:
        """
        Initialize the launcher.

        :param image: Optional registry image reference to provision
            sandboxes from, e.g. ``"docker.io/me/omnigent-host:latest"``
            — the server's managed-host ``sandbox.daytona.image``
            config. ``None`` resolves :data:`HOST_IMAGE_ENV_VAR` and
            falls back to the official
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param env: Optional names of server-process environment
            variables to inject into every sandbox, e.g.
            ``["OPENAI_API_KEY", "GIT_TOKEN"]`` — the server's
            managed-host ``sandbox.daytona.env`` config. ``None``
            resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`
            (comma-separated) and falls back to no injected env.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._client: daytona_sdk.Daytona | None = None
        self._sandboxes: dict[str, DaytonaSandbox] = {}
        self._platform_owner: str | None = None
        self._platform_session_id: str | None = None

    def _daytona(self) -> daytona_sdk.Daytona:
        """
        Return the (lazily created) Daytona API client.

        The client reads ``DAYTONA_API_KEY`` / ``DAYTONA_API_URL`` /
        ``DAYTONA_TARGET`` from the process environment — the same
        12-factor posture as the Modal launcher's credentials.

        :returns: The shared client instance.
        """
        if self._client is None:
            import daytona

            self._client = daytona.Daytona()
        return self._client

    def _resolve(self, sandbox_id: str) -> DaytonaSandbox:
        """
        Return the cached handle for *sandbox_id*, looking it up on
        first use.

        :param sandbox_id: Daytona sandbox id (a UUID string).
        :returns: The sandbox handle.
        :raises click.ClickException: When the SDK is not installed or
            the sandbox does not exist.
        """
        # The CLI connect flow reaches primitives without a prepare()
        # preflight — ensure the missing-SDK error stays the friendly
        # install hint rather than a raw ImportError.
        _ensure_sdk()
        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            import daytona

            try:
                handle = self._daytona().get(sandbox_id)
            except daytona.DaytonaNotFoundError as exc:
                raise click.ClickException(
                    f"Daytona sandbox '{sandbox_id}' not found — it may have "
                    "been deleted. Managed sessions provision a replacement "
                    "on the next message."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a credential the
        deployment never provided; silently launching without it would
        surface much later as an opaque harness auth failure).

        :returns: Name → value mapping for ``env_vars`` at creation.
        :raises click.ClickException: When a configured name is not set
            in the server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    "sandbox.daytona.env / "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def set_platform_owner(self, owner: str) -> None:
        """Bind this per-launch instance to the authenticated session owner."""
        self._platform_owner = owner

    def set_platform_session(self, session_id: str) -> None:
        """Bind provider metadata to the Omnigent session being launched."""
        self._platform_session_id = session_id

    def _resolve_owner_profile(self) -> dict[str, object]:
        """Resolve non-secret Daytona mount references for this owner."""
        base_url = os.environ.get("PLATFORM_MODEL_CREDENTIAL_BROKER_URL")
        token = os.environ.get("PLATFORM_MODEL_CREDENTIAL_BROKER_TOKEN")
        if not base_url and not token:
            return {}
        if not base_url or not token or not self._platform_owner:
            raise click.ClickException(
                "owner-scoped model credential broker is incompletely configured"
            )
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}{urlencode({'owner': self._platform_owner})}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                profile = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise click.ClickException(
                "could not resolve the execution owner's model connection profile"
            ) from exc
        if not isinstance(profile, dict):
            raise click.ClickException("model connection broker returned an invalid profile")
        return profile

    @staticmethod
    def _append_runner_passthrough(env_vars: dict[str, str]) -> None:
        names = [
            item.strip()
            for item in env_vars.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "").split(",")
            if item.strip()
        ]
        for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            if name not in names:
                names.append(name)
        env_vars["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(names)

    def prepare(self) -> None:
        """
        Local preflight: the Daytona SDK must be installed and an API
        key available.

        :raises click.ClickException: When the SDK is missing or
            ``DAYTONA_API_KEY`` is not set.
        """
        _ensure_sdk()
        if not os.environ.get("DAYTONA_API_KEY"):
            raise click.ClickException(
                "No Daytona credentials found. Create an API key at "
                "https://app.daytona.io and set DAYTONA_API_KEY."
            )

    def provision(self, name: str) -> str:
        """
        Create a new Daytona sandbox from the host image.

        Idle auto-stop is set to one hour. A stopped sandbox retains its
        filesystem and is resumed in place by the managed-session wake path,
        while truly abandoned sessions stop consuming compute. The first
        creation from a given image is slow (Daytona pulls it and builds an
        internal snapshot); later creations reuse the snapshot.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Recorded as a label; the returned id is the canonical
            reference.
        :returns: The sandbox id (a UUID string).
        """
        _ensure_sdk()
        import daytona

        resolved_ref = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        env_vars = self._resolve_sandbox_env()
        profile = self._resolve_owner_profile()
        env_vars.setdefault("CODEX_HOME", "/root/.codex")
        env_vars.setdefault("CLAUDE_CONFIG_DIR", "/root/.claude")
        self._append_runner_passthrough(env_vars)
        volumes = None
        raw_volume = profile.get("volume")
        if raw_volume is not None:
            if not isinstance(raw_volume, dict):
                raise click.ClickException("model connection profile volume is invalid")
            volume_id = raw_volume.get("id")
            subpath = raw_volume.get("subpath")
            mount_path = raw_volume.get("mountPath", "/root/.tellimer-auth")
            if not all(
                isinstance(value, str) and value for value in (volume_id, subpath, mount_path)
            ):
                raise click.ClickException("model connection profile volume is incomplete")
            volumes = [
                daytona.VolumeMount(
                    volume_id=volume_id,
                    mount_path=mount_path,
                    subpath=subpath,
                )
            ]
        raw_secrets = profile.get("secrets", {})
        if not isinstance(raw_secrets, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_secrets.items()
        ):
            raise click.ClickException("model connection profile secrets are invalid")
        labels = {"omnigent-name": name}
        if self._platform_session_id is not None:
            # Provider-dashboard audit trail: the canonical session id makes
            # the one-session/one-sandbox ownership visible outside Omnigent.
            labels["omnigent-session-id"] = self._platform_session_id
        click.echo(f"▸ Creating Daytona sandbox '{name}' from {resolved_ref}")
        try:
            handle = self._daytona().create(
                daytona.CreateSandboxFromImageParams(
                    image=resolved_ref,
                    env_vars=env_vars or None,
                    labels=labels,
                    # Release compute after one hour of inactivity. The
                    # persistent sandbox is resumed in place on next message.
                    auto_stop_interval=_MANAGED_AUTO_STOP_MINUTES,
                    resources=daytona.Resources(cpu=_SANDBOX_CPU, memory=_SANDBOX_MEMORY_GIB),
                    volumes=volumes,
                    secrets=raw_secrets or None,
                ),
                timeout=_CREATE_TIMEOUT_S,
                # First-use image pulls stream build logs; echo them so a
                # slow cold create is visibly progressing in the server log.
                on_snapshot_create_logs=click.echo,
            )
        except daytona.DaytonaError as exc:
            # SDK boundary: surface the provider's reason (quota, image
            # pull failure, "verify your email" account suspensions, …)
            # as the launcher-contract error type so the managed-launch
            # 502 — and a waiting message POST — carries it verbatim
            # instead of a generic "internal error".
            raise click.ClickException(f"Daytona sandbox creation failed: {exc}") from exc
        self._sandboxes[handle.id] = handle
        if raw_volume is not None:
            self.run(
                handle.id,
                "set -eu; mkdir -p /root/.codex; chmod 700 /root/.codex; "
                "if [ -s /root/.tellimer-auth/codex/auth.json ]; then "
                "cp -f /root/.tellimer-auth/codex/auth.json /root/.codex/auth.json; "
                "chmod 600 /root/.codex/auth.json; fi; "
                "if [ -f /root/.tellimer-auth/codex/config.toml ]; then "
                "cp -f /root/.tellimer-auth/codex/config.toml /root/.codex/config.toml; fi; "
                "rm -f /root/.tellimer-auth/codex/state_*.sqlite*",
            )
            self.run_background(
                handle.id,
                "while sleep 15; do "
                "if [ -s /root/.codex/auth.json ]; then "
                "mkdir -p /root/.tellimer-auth/codex; "
                "tmp=/root/.tellimer-auth/codex/.auth.json.$$.tmp; "
                "cp -f /root/.codex/auth.json $tmp && chmod 600 $tmp "
                "&& mv -f $tmp /root/.tellimer-auth/codex/auth.json; "
                "fi; done",
                log_path="/tmp/codex-auth-sync.log",
            )
        click.echo(f"  → created {handle.id}")
        return handle.id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing sandbox, starting it if stopped.

        Unlike Modal (whose terminated sandboxes are gone for good),
        a stopped Daytona sandbox can be restarted — e.g. one created
        outside this flow whose idle auto-stop kicked in — so attach
        starts it rather than rejecting it.

        :param sandbox_id: The sandbox to attach to (a UUID string).
        :raises click.ClickException: When the sandbox does not exist
            or cannot be started.
        """
        click.echo(f"▸ Reusing existing Daytona sandbox '{sandbox_id}'")
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.refresh_data()
            if handle.state != daytona.SandboxState.STARTED:
                click.echo(f"  → starting sandbox (state: {handle.state})")
                handle.start()
        except daytona.DaytonaError as exc:
            # SDK boundary: surface the provider's reason (e.g. a
            # sandbox stuck in ERROR state) through the launcher
            # contract instead of a raw SDK traceback.
            raise click.ClickException(
                f"Could not attach to Daytona sandbox '{sandbox_id}': {exc}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Disable Daytona's idle auto-stop so the host survives idle
        gaps between turns.

        Managed ``provision`` uses a one-hour idle window. This separate
        CLI-bootstrap primitive disables auto-stop for an explicitly attached
        foreground host. Soft-fail per the launcher contract: a rejected
        setting warns rather than aborting the bootstrap.

        :param sandbox_id: The sandbox to configure.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.set_autostop_interval(_AUTO_STOP_DISABLED)
        except daytona.DaytonaError as exc:
            ui.console.print(
                f"  → warning: could not disable idle auto-stop on "
                f"'{sandbox_id}' ({exc}); the sandbox may stop after "
                "Daytona's idle timeout.",
                style="omni.warning",
                markup=False,
            )
        else:
            click.echo("  → idle auto-stop disabled (sandbox lives until deleted)")

    def resume(self, sandbox_id: str) -> None:
        """Start a stopped Daytona sandbox in place, preserving its filesystem."""
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.refresh_data()
            if handle.state != daytona.SandboxState.STARTED:
                handle.start()
            # Old Omnigent generations were created with auto-stop disabled.
            # Re-assert the managed one-hour policy on every wake so they
            # converge without destructive migration.
            handle.set_autostop_interval(_MANAGED_AUTO_STOP_MINUTES)
        except daytona.DaytonaError as exc:
            raise click.ClickException(
                f"Could not resume Daytona sandbox '{sandbox_id}': {exc}"
            ) from exc

    def is_running(self, sandbox_id: str) -> bool | None:
        """Return Daytona's refreshed started/stopped state for wake routing."""
        try:
            handle = self._resolve(sandbox_id)
        except click.ClickException:
            # The managed relaunch path will attempt a fresh generation when
            # the old provider object is gone. Report not-running rather than
            # letting a status probe abort message dispatch.
            return False
        import daytona

        try:
            handle.refresh_data()
        except daytona.DaytonaError:
            # A transient provider read must not be mistaken for a definitive
            # stopped/deleted state. Omnigent falls back to tunnel liveness.
            return None
        if handle.state == daytona.SandboxState.STARTED:
            return True
        if handle.state == daytona.SandboxState.STOPPED:
            return False
        return None

    def exists(self, sandbox_id: str) -> bool | None:
        """Return whether Daytona still retains the named sandbox."""
        _ensure_sdk()
        import daytona

        try:
            self._daytona().get(sandbox_id)
        except daytona.DaytonaNotFoundError:
            self._sandboxes.pop(sandbox_id, None)
            return False
        except daytona.DaytonaError:
            return None
        return True

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        Daytona's toolbox merges stdout and stderr into one stream, so
        the combined output lands in ``stdout`` and ``stderr`` is
        always empty (the documented
        :class:`~omnigent.onboarding.sandboxes.base.RemoteCommandResult`
        merged-streams convention).

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured combined output.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """
        import daytona

        handle = self._resolve(sandbox_id)
        try:
            response = handle.process.exec(command)
        except daytona.DaytonaError as exc:
            # SDK boundary: a stopped/deleted sandbox or toolbox outage
            # must surface its provider reason through the launcher
            # contract, not as a raw SDK exception the managed flow
            # reports as "internal error".
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        output = response.result or ""
        for line in output.splitlines():
            if line.strip():
                click.echo(line)
        if check and response.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {response.exit_code}): {command}"
            )
        return RemoteCommandResult(returncode=response.exit_code, stdout=output, stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the SDK's filesystem
        API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.fs.upload_file(str(local_path), remote_path)
        except daytona.DaytonaError as exc:
            # SDK boundary: a stopped sandbox or toolbox outage must
            # surface its provider reason through the launcher contract.
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox over a PTY session, echoing its
        output to the local terminal until it exits; Ctrl-C kills the
        remote process and re-raises.

        The PTY session spawns a shell; the command is sent as a
        single input line with ``exec`` so the shell is replaced and
        the PTY closes (carrying the command's exit code in its close
        frame) when the command exits. ``TERM`` is forced to
        ``xterm-256color`` for the same reason as the Modal launcher:
        native harnesses spawn tmux, which refuses to start under a
        dumb/unset TERM.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises click.ClickException: When the PTY session cannot be
            created or ends without reporting an exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote
            process when the user detaches with Ctrl-C.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        # PTY session ids must be unique within the sandbox; a fresh
        # suffix per call lets connect be re-run after a detach.
        session_id = f"oa-foreground-{uuid.uuid4().hex[:8]}"
        try:
            pty = handle.process.create_pty_session(id=session_id)
        except daytona.DaytonaError as exc:
            raise click.ClickException(
                f"Could not open a PTY session on sandbox '{sandbox_id}': {exc}"
            ) from exc
        return _drive_foreground_pty(pty, sandbox_id, command)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the
        prebaked host image — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale.

        :param remote_tgz_path: Sandbox path of the shipped tarball,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no
        longer exists is treated as success — the desired end state
        holds. A delete that races another state change (Daytona
        reports ``DaytonaConflictError: Sandbox state change in
        progress`` — observed live when two cleanup paths overlap) is
        retried briefly; a deletion already in flight resolves to
        not-found on a later attempt.

        :param sandbox_id: The sandbox to delete.
        :raises daytona.DaytonaError: When the delete still conflicts
            after the retries (callers in the managed teardown path
            are best-effort and log it).
        """
        _ensure_sdk()
        import daytona

        # Hand-rolled bounded retry on purpose: the retry condition is
        # one provider-specific exception in one place, and tenacity is
        # not an omnigent dependency — pulling it in for a 3-iteration
        # loop fails the cost/benefit test.
        for attempt in range(_TERMINATE_CONFLICT_RETRIES):
            try:
                handle = self._daytona().get(sandbox_id)
            except daytona.DaytonaNotFoundError:
                break
            try:
                self._daytona().delete(handle)
                break
            except daytona.DaytonaConflictError:
                if attempt == _TERMINATE_CONFLICT_RETRIES - 1:
                    raise
                time.sleep(_TERMINATE_CONFLICT_BACKOFF_S)
        self._sandboxes.pop(sandbox_id, None)
