"""Path resolution for panic-monitor state.

Three roots:
  CONFIG  — secrets + trust log + peers cache  (XDG_CONFIG_HOME or /etc)
  DATA    — SQLite history + logstore           (XDG_DATA_HOME   or /var/lib)
  RUNTIME — control socket + pidfile            (XDG_RUNTIME_DIR or /run)

Root resolution order:
  1. env override (``$PANIC_MONITOR_CONFIG_DIR`` / ``$PANIC_MONITOR_DATA_DIR``)
  2. system mode (euid == 0) -> ``/etc`` / ``/var/lib`` / ``/run``
  3. XDG user mode

Resolvers are evaluated each call so callers under test can reset env vars
between cases.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "panic-monitor"

ENV_CONFIG_DIR = "PANIC_MONITOR_CONFIG_DIR"
ENV_DATA_DIR = "PANIC_MONITOR_DATA_DIR"


def system_mode() -> bool:
    """True when running as root — switches defaults to the FHS layout."""
    return os.geteuid() == 0


def config_dir() -> Path:
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override)
    if system_mode():
        return Path("/etc") / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override)
    if system_mode():
        return Path("/var/lib") / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def runtime_dir() -> Path:
    if system_mode():
        return Path("/run") / APP_NAME
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP_NAME
    # Fallback for environments without XDG_RUNTIME_DIR (rare on systemd hosts).
    return Path("/tmp") / f"{APP_NAME}-{os.getuid()}"


def ensure_runtime_dir() -> Path:
    """Create the runtime dir at 0700 (owner-only) and return its path."""
    p = runtime_dir()
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


# Convenience canonical filenames (callers can build paths with these so the
# names live in one place).
SECRET_KEY_NAME = "secret.key"
SECRET_META_NAME = "secret.meta"
TRUST_LOG_NAME = "log.jsonl"
PEERS_CACHE_NAME = "peers.json"
HISTORY_DB_NAME = "history.db"
LOGSTORE_DB_NAME = "logstore.db"
CONTROL_SOCKET_NAME = "control.sock"
# machine-id password backend: SecretBox ciphertext + its argon2 salt
PASSWORD_ENC_NAME = "password.enc"
PASSWORD_SALT_NAME = "password.salt"


def default_identity_path() -> Path:
    return config_dir() / SECRET_KEY_NAME


def default_meta_path() -> Path:
    return config_dir() / SECRET_META_NAME


def default_log_path() -> Path:
    return config_dir() / TRUST_LOG_NAME


def default_peers_path() -> Path:
    return config_dir() / PEERS_CACHE_NAME


def default_history_path() -> Path:
    return data_dir() / HISTORY_DB_NAME


def default_logstore_path() -> Path:
    return data_dir() / LOGSTORE_DB_NAME


def default_control_socket() -> Path:
    return runtime_dir() / CONTROL_SOCKET_NAME
