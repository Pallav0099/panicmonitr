"""Pluggable password providers for the daemon and CLI.

Backends:
  - ``systemd-creds``: read from ``$CREDENTIALS_DIRECTORY/panic-monitor-password``
  - ``machine-id``:    decrypt ``password.enc`` with a key derived from
                       ``/etc/machine-id`` — works on any distro, headless, with
                       no systemd-creds and no D-Bus (the portable fallback)
  - ``keyring``:       OS keyring via the ``keyring`` package (opt-in)
  - ``stdin``:         read a single line from stdin (docker / piped invocation)
  - ``env``:           ``PANIC_MONITOR_PASSWORD`` env var (back-compat; warns)
  - ``pinentry``:      interactive on a TTY (getpass)

Selection priority:
  1. Explicit ``--password-from``
  2. ``$PANIC_MONITOR_PASSWORD_FROM`` env var
  3. ``systemd-creds`` if ``$CREDENTIALS_DIRECTORY`` is set
  4. ``machine-id`` if an encrypted ``password.enc`` exists in the config dir
  5. ``env`` if ``$PANIC_MONITOR_PASSWORD`` is set
  6. ``pinentry`` if stdin is a TTY
  7. ``stdin`` otherwise
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import Protocol

import nacl.exceptions
import nacl.secret
import nacl.utils
from argon2.low_level import Type, hash_secret_raw
from loguru import logger

from src import paths

PASSWORD_ENV = "PANIC_MONITOR_PASSWORD"
PASSWORD_FROM_ENV = "PANIC_MONITOR_PASSWORD_FROM"
CRED_NAME = "panic-monitor-password"

# machine-id backend: argon2id(machine-id, salt) -> 32-byte SecretBox key.
# machine-id is high-entropy (128-bit), so the argon2 cost is for consistency
# with identity sealing rather than low-entropy stretching; keep memory modest
# so the daemon's startup KDF stays well under the unit's MemoryMax.
_KDF_TIME_COST = 3
_KDF_MEMORY_COST = 32768  # 32 MiB
_KDF_PARALLELISM = 4
_KEY_LEN = 32
_SALT_LEN = 16


def _machine_id() -> bytes:
    """Host-stable secret that binds the encrypted password to this machine."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        if value:
            return value.encode()
    raise RuntimeError(
        "no machine-id found (/etc/machine-id missing) — the machine-id backend "
        "can't run here; use --password-from systemd-creds|keyring|env"
    )


def _derive_key(salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=_machine_id(),
        salt=salt,
        time_cost=_KDF_TIME_COST,
        memory_cost=_KDF_MEMORY_COST,
        parallelism=_KDF_PARALLELISM,
        hash_len=_KEY_LEN,
        type=Type.ID,
    )


def seal_password_to_disk(password: str, config_dir: Path) -> Path:
    """Encrypt *password* at rest, bound to this host's machine-id.

    Writes ``password.enc`` (SecretBox ciphertext) and ``password.salt`` (the
    argon2 salt) into *config_dir*, both mode 0600. Returns the ciphertext path.

    Security note: this is encryption *at rest* + host binding — the ciphertext
    is useless if copied to another machine. It does NOT protect the password
    from a process already running as this user with read access to the config
    dir and /etc/machine-id (which is world-readable). For stronger isolation on
    systemd >= 256, prefer the systemd-creds backend.
    """
    salt = nacl.utils.random(_SALT_LEN)
    key = _derive_key(salt)
    ciphertext = nacl.secret.SecretBox(key).encrypt(password.encode())
    config_dir.mkdir(parents=True, exist_ok=True)
    salt_path = config_dir / paths.PASSWORD_SALT_NAME
    enc_path = config_dir / paths.PASSWORD_ENC_NAME
    salt_path.write_bytes(salt)
    enc_path.write_bytes(ciphertext)
    for p in (salt_path, enc_path):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return enc_path


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


class MachineIdProvider:
    """Decrypts ``password.enc`` with a key derived from ``/etc/machine-id``.

    The portable fallback: works on any Linux host with no systemd-creds and no
    D-Bus/keyring, so it covers older systemd (< 256 in user mode) and headless
    servers. See :func:`seal_password_to_disk` for the security model.
    """

    def get(self) -> str:
        cfg = paths.config_dir()
        enc_path = cfg / paths.PASSWORD_ENC_NAME
        salt_path = cfg / paths.PASSWORD_SALT_NAME
        if not enc_path.exists() or not salt_path.exists():
            raise RuntimeError(
                f"encrypted password not found at {enc_path} — run "
                "'panic-monitor --install-service --password-from machine-id'"
            )
        key = _derive_key(salt_path.read_bytes())
        try:
            return nacl.secret.SecretBox(key).decrypt(enc_path.read_bytes()).decode()
        except nacl.exceptions.CryptoError as exc:
            raise RuntimeError(
                "failed to decrypt password — has /etc/machine-id changed? "
                "Re-run 'panic-monitor --install-service --password-from machine-id "
                "--rotate-password'"
            ) from exc


class PinentryProvider:
    """Interactive prompt on a TTY via getpass."""

    def get(self) -> str:
        try:
            return getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("password prompt aborted")


_BACKENDS = {
    "systemd-creds": SystemdCredsProvider,
    "machine-id": MachineIdProvider,
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
    if (paths.config_dir() / paths.PASSWORD_ENC_NAME).exists():
        return "machine-id"
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
