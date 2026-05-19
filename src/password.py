"""Pluggable password providers for the daemon and CLI.

Backends:
  - ``systemd-creds``: read from ``$CREDENTIALS_DIRECTORY/panic-monitor-password``
  - ``keyring``:       OS keyring via the ``keyring`` package (opt-in)
  - ``stdin``:         read a single line from stdin (docker / piped invocation)
  - ``env``:           ``PANIC_MONITOR_PASSWORD`` env var (back-compat; warns)
  - ``pinentry``:      interactive on a TTY (getpass)

Selection priority:
  1. Explicit ``--password-from``
  2. ``$PANIC_MONITOR_PASSWORD_FROM`` env var
  3. ``systemd-creds`` if ``$CREDENTIALS_DIRECTORY`` is set
  4. ``env`` if ``$PANIC_MONITOR_PASSWORD`` is set
  5. ``pinentry`` if stdin is a TTY
  6. ``stdin`` otherwise
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import Protocol

from loguru import logger

PASSWORD_ENV = "PANIC_MONITOR_PASSWORD"
PASSWORD_FROM_ENV = "PANIC_MONITOR_PASSWORD_FROM"
CRED_NAME = "panic-monitor-password"


class PasswordProvider(Protocol):
    def get(self) -> str: ...


class StdinProvider:
    def get(self) -> str:
        data = sys.stdin.readline()
        if not data:
            raise RuntimeError("no password on stdin")
        return data.rstrip("\n")


class EnvVarProvider:
    """Reads ``PANIC_MONITOR_PASSWORD``. Kept for back-compat — warns at use."""

    def get(self) -> str:
        val = os.environ.get(PASSWORD_ENV)
        if not val:
            raise RuntimeError(f"{PASSWORD_ENV} is not set")
        logger.warning(
            "Using plaintext env-var password. Run "
            "'panic-monitor --install-service --rotate-password' to switch to "
            "systemd-creds."
        )
        return val


class KeyringProvider:
    """Reads from the OS keyring under (service='panic-monitor', user=<euid>)."""

    SERVICE = "panic-monitor"

    def get(self) -> str:
        try:
            import keyring  # type: ignore
        except ImportError as exc:
            raise RuntimeError("keyring package not installed") from exc
        user = str(os.geteuid())
        pw = keyring.get_password(self.SERVICE, user)
        if not pw:
            raise RuntimeError(
                f"no keyring entry for service={self.SERVICE} user={user}"
            )
        return pw


class SystemdCredsProvider:
    """Reads ``$CREDENTIALS_DIRECTORY/panic-monitor-password``.

    systemd-creds decrypts the credential and exposes the plaintext in a
    private tmpfs at this path before invoking ExecStart. The file is
    automatically unlinked when the unit stops.
    """

    def get(self) -> str:
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if not cred_dir:
            raise RuntimeError(
                "$CREDENTIALS_DIRECTORY not set — unit needs "
                "LoadCredentialEncrypted=panic-monitor-password:..."
            )
        path = Path(cred_dir) / CRED_NAME
        if not path.exists():
            raise RuntimeError(f"credential file missing at {path}")
        return path.read_text().rstrip("\n")


class PinentryProvider:
    """Interactive prompt on a TTY via getpass."""

    def get(self) -> str:
        try:
            return getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("password prompt aborted")


_BACKENDS = {
    "systemd-creds": SystemdCredsProvider,
    "keyring": KeyringProvider,
    "stdin": StdinProvider,
    "env": EnvVarProvider,
    "pinentry": PinentryProvider,
}


def select_backend(explicit: str | None = None) -> str:
    """Resolve the backend name using the priority documented above."""
    if explicit:
        if explicit not in _BACKENDS:
            raise ValueError(f"unknown password backend: {explicit}")
        return explicit
    env = os.environ.get(PASSWORD_FROM_ENV)
    if env:
        if env not in _BACKENDS:
            raise ValueError(f"unknown password backend in {PASSWORD_FROM_ENV}: {env}")
        return env
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        cred = Path(os.environ["CREDENTIALS_DIRECTORY"]) / CRED_NAME
        if cred.exists():
            return "systemd-creds"
    if os.environ.get(PASSWORD_ENV):
        return "env"
    if sys.stdin.isatty():
        return "pinentry"
    return "stdin"


def get_password(explicit_backend: str | None = None) -> str:
    """Return the password using the resolved backend."""
    name = select_backend(explicit_backend)
    cls = _BACKENDS[name]
    logger.debug("[password] backend={}", name)
    return cls().get()
