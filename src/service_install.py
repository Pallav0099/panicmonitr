"""`panic-monitor install-service` and `migrate` implementations.

User mode (default) writes ``~/.config/systemd/user/panic-monitor.service``
and uses ``systemctl --user``. System mode (when running as root) writes
``/etc/systemd/system/panic-monitor.service`` and uses ``systemctl``.

The unit template is rendered with the install-time values for executable
path, state directories, and credential backend so the generated unit is
self-contained — no path-mismatch between what's installed and what the
daemon actually reads.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from src import paths
from src.sysenv import system_env

SERVICE_NAME = "panic-monitor.service"


def _resource_base() -> Path:
    """Base directory for bundled package resources.

    Under a frozen PyInstaller build, data files are unpacked beneath
    ``sys._MEIPASS`` (the definitive bundle marker); in a normal install they
    sit next to this module.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass) / "src"
    return Path(__file__).resolve().parent


TEMPLATE_PATH = _resource_base() / "templates" / "panic-monitor.service.tmpl"

LEGACY_PATHS = [
    Path.cwd() / "secret.key",
    Path.cwd() / "secret.meta",
    Path.cwd() / "peers.json",
    Path.cwd() / "log.jsonl",
    Path.cwd() / "history.db",
    Path.cwd() / "logstore.db",
    Path("/etc/panic-monitor/secret.key"),
    Path("/etc/panic-monitor/secret.meta"),
    Path("/etc/panic-monitor/peers.json"),
    Path("/etc/panic-monitor/log.jsonl"),
    Path("/var/lib/panic-monitor/history.db"),
    Path("/var/lib/panic-monitor/logstore.db"),
]


def _resolve_exec_start() -> str:
    """Locate the `panic-monitor` entrypoint to bake into ExecStart.

    When running as a frozen PyInstaller binary the executable re-invokes
    itself by its own path — ``sys.executable`` *is* the installed binary, so
    use it directly. Otherwise prefer ``shutil.which`` so a venv-installed
    console script resolves to its absolute path, falling back to running
    ``main.py`` under the active python for a bare source checkout.
    """
    if getattr(sys, "frozen", False):
        return os.path.realpath(sys.executable)
    found = shutil.which("panic-monitor")
    if found:
        return found
    # Fallback: invoke main.py via the active python (source checkout).
    return f"{sys.executable} {Path(__file__).resolve().parent.parent / 'main.py'}"


def _unit_dir(*, system: bool) -> Path:
    if system:
        return Path("/etc/systemd/system")
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _systemctl(*args: str, system: bool) -> subprocess.CompletedProcess:
    cmd = ["systemctl"] + ([] if system else ["--user"]) + list(args)
    logger.debug("[install-service] $ {}", " ".join(cmd))
    return subprocess.run(cmd, check=False, env=system_env())


def _systemd_version() -> int | None:
    """Best-effort major systemd version, or None if it can't be determined."""
    if shutil.which("systemctl") is None:
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "--version"],
            check=False, capture_output=True, text=True, env=system_env(),
        )
    except OSError:
        return None
    first_line = (proc.stdout or "").split("\n", 1)[0]  # "systemd 257 (257.5-1)"
    for token in first_line.split():
        if token.isdigit():
            return int(token)
    return None


def _systemd_creds_viable(*, system: bool) -> bool:
    """Whether systemd-creds will actually work for this install mode.

    Needs the binary (systemd >= 250) and, in user mode, version >= 256 so the
    per-user systemd instance can decrypt a user-scoped credential. When the
    version can't be read, assume OK for system mode but be cautious (False) for
    user mode so we fall back to a backend that definitely works.
    """
    if shutil.which("systemd-creds") is None:
        return False
    ver = _systemd_version()
    if ver is None:
        return system
    return ver >= 250 if system else ver >= 256


def _render_unit(
    *,
    exec_start: str,
    config_dir: Path,
    data_dir: Path,
    password_backend: str,
    credential_file: Path | None,
    system: bool,
) -> str:
    template = TEMPLATE_PATH.read_text()

    if password_backend == "systemd-creds" and credential_file is not None:
        credential_directive = (
            f"LoadCredentialEncrypted=panic-monitor-password:{credential_file}\n"
        )
        password_env_directive = ""
    elif password_backend == "env":
        credential_directive = ""
        # Existing deployments may still set this externally; we don't bake a
        # plaintext value into the unit. See README for the migration path.
        password_env_directive = ""
    else:
        credential_directive = ""
        password_env_directive = ""

    # Hardening that touches the capability bounding set (Restrict*, Protect*
    # kernel surfaces) only works when the launching systemd instance holds
    # CAP_SETPCAP. The per-user instance doesn't, so we keep the privileged
    # block for system mode and omit it for user mode — without this the unit
    # fails at the CAPABILITIES step with "Operation not permitted".
    #
    # ProtectHome= is also system-only here: in user mode the daemon needs to
    # read its own ~/.config/panic-monitor, but ProtectHome=tmpfs masks the
    # entire home tree before the ReadWritePaths bind-mount can resolve. The
    # user-mode service runs *as* the user anyway, so hiding /home from itself
    # buys no security.
    if system:
        extra_hardening = (
            "ProtectHome=yes\n"
            "ProtectKernelTunables=yes\n"
            "ProtectKernelModules=yes\n"
            "ProtectControlGroups=yes\n"
            "RestrictSUIDSGID=yes\n"
            "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n"
            "SystemCallFilter=@system-service\n"
            "SystemCallArchitectures=native\n"
        )
    else:
        extra_hardening = ""

    return template.format(
        exec_start=exec_start,
        identity_path=config_dir / paths.SECRET_KEY_NAME,
        identity_meta_path=config_dir / paths.SECRET_META_NAME,
        peers_path=config_dir / paths.PEERS_CACHE_NAME,
        log_path=config_dir / paths.TRUST_LOG_NAME,
        history_db=data_dir / paths.HISTORY_DB_NAME,
        logstore_db=data_dir / paths.LOGSTORE_DB_NAME,
        password_backend=password_backend,
        config_dir=config_dir,
        data_dir=data_dir,
        working_dir=config_dir,
        credential_directive=credential_directive,
        password_env_directive=password_env_directive,
        read_write_paths=f"{config_dir} {data_dir}",
        wanted_by="multi-user.target" if system else "default.target",
        extra_hardening=extra_hardening,
    )


def _encrypt_credential(password: str, dest: Path, *, system: bool) -> None:
    """Encrypt ``password`` with systemd-creds and write to ``dest``.

    User-mode installs pass ``--user`` so the resulting credential is decryptable
    by the per-user systemd instance. Without it, encryption defaults to the
    host key at ``/var/lib/systemd/credential.secret`` (root-only), and user
    services fail at the ``CREDENTIALS`` step with "Wrong medium type".
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["systemd-creds", "encrypt", "--name=panic-monitor-password"]
    if not system:
        cmd.append("--user")
    cmd += ["-", str(dest)]
    proc = subprocess.run(
        cmd,
        input=password.encode(),
        check=False,
        capture_output=True,
        env=system_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"systemd-creds encrypt failed (exit {proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def _prompt_password(label: str = "identity password: ") -> str:
    try:
        return getpass.getpass(label)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)


def install_service(
    *,
    system: bool | None = None,
    force: bool = False,
    password_backend: str | None = None,
    rotate_password: bool = False,
) -> int:
    """Render the unit, write it, and enable + start the service.

    ``password_backend=None`` auto-selects: systemd-creds when it's actually
    usable for this mode (systemd >= 250 system / >= 256 user), otherwise the
    portable machine-id encrypted-file backend, which works on any distro and
    headless. Returns 0 on success, non-zero on failure.
    """
    if system is None:
        system = paths.system_mode()

    if password_backend is None:
        password_backend = (
            "systemd-creds" if _systemd_creds_viable(system=system) else "machine-id"
        )
        ver = _systemd_version()
        why = f"systemd {ver}" if ver is not None else "systemd version unknown"
        print(f"Auto-selected password backend: {password_backend} ({why}, "
              f"{'system' if system else 'user'} mode).")

    unit_dir = _unit_dir(system=system)
    unit_path = unit_dir / SERVICE_NAME
    config_dir = paths.config_dir()
    data_dir = paths.data_dir()

    if unit_path.exists() and not force and not rotate_password:
        print(f"Refusing to overwrite existing unit at {unit_path}. Use --force.", file=sys.stderr)
        return 1

    # Credential setup ------------------------------------------------------
    credential_file: Path | None = None
    if password_backend == "systemd-creds":
        if shutil.which("systemd-creds") is None:
            print(
                "systemd-creds not found on PATH. Install systemd >= 250, or use "
                "--password-from machine-id (works anywhere) or another backend.",
                file=sys.stderr,
            )
            return 1
        credential_file = config_dir / "password.cred"
        if not credential_file.exists() or rotate_password:
            print("Enter the identity password (will be encrypted with systemd-creds).")
            password = _prompt_password()
            try:
                _encrypt_credential(password, credential_file, system=system)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Encrypted credential written to {credential_file}")
    elif password_backend == "machine-id":
        from src.password import seal_password_to_disk

        enc_path = config_dir / paths.PASSWORD_ENC_NAME
        if not enc_path.exists() or rotate_password:
            print("Enter the identity password (encrypted at rest, bound to this host's machine-id).")
            password = _prompt_password()
            try:
                seal_password_to_disk(password, config_dir)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Encrypted credential written to {enc_path}")
    elif password_backend == "keyring":
        try:
            import keyring
        except ImportError:
            print(
                "The 'keyring' package is not installed. Please install it with:\n"
                "pip install keyring",
                file=sys.stderr,
            )
            return 1
        print("Enter the identity password to store in the OS keyring.")
        password = _prompt_password()
        try:
            import os
            keyring.set_password("panic-monitor", str(os.geteuid()), password)
            print("Password successfully stored in keyring.")
        except Exception as exc:
            print(f"Failed to store password in keyring: {exc}", file=sys.stderr)
            return 1

    # Render + write unit ---------------------------------------------------
    exec_start = _resolve_exec_start()
    content = _render_unit(
        exec_start=exec_start,
        config_dir=config_dir,
        data_dir=data_dir,
        password_backend=password_backend,
        credential_file=credential_file,
        system=system,
    )
    unit_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(content)
    print(f"Wrote {unit_path}")

    # Reload + enable + start ----------------------------------------------
    if _systemctl("daemon-reload", system=system).returncode != 0:
        print("daemon-reload failed", file=sys.stderr)
        return 1
    if _systemctl("enable", "--now", SERVICE_NAME, system=system).returncode != 0:
        print(f"Failed to enable+start {SERVICE_NAME}", file=sys.stderr)
        return 1

    mode = "system" if system else "user"
    print(f"\n{SERVICE_NAME} enabled and started ({mode} mode).")
    print(f"  state config: {config_dir}")
    print(f"  state data:   {data_dir}")
    print(f"  password:     {password_backend}")
    print(f"\nFollow logs: journalctl {'' if system else '--user '}-u {SERVICE_NAME} -f")
    return 0


def uninstall_service(*, system: bool | None = None) -> int:
    if system is None:
        system = paths.system_mode()
    unit_dir = _unit_dir(system=system)
    unit_path = unit_dir / SERVICE_NAME

    _systemctl("disable", "--now", SERVICE_NAME, system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f"Removed {unit_path}")
    _systemctl("daemon-reload", system=system)
    return 0


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

_CONFIG_FILES = {
    paths.SECRET_KEY_NAME,
    paths.SECRET_META_NAME,
    paths.PEERS_CACHE_NAME,
    paths.TRUST_LOG_NAME,
}
_DATA_FILES = {
    paths.HISTORY_DB_NAME,
    paths.LOGSTORE_DB_NAME,
}


def _is_legacy_candidate(p: Path) -> bool:
    """True if ``p`` is a real file that lives outside the resolved XDG roots."""
    if not p.exists() or not p.is_file():
        return False
    cfg = paths.config_dir().resolve()
    data = paths.data_dir().resolve()
    rp = p.resolve()
    return cfg not in rp.parents and data not in rp.parents and rp != cfg and rp != data


def migrate_legacy_state() -> int:
    """Copy files from legacy locations (CWD, /etc, /var/lib) into XDG paths.

    Non-destructive: source files are left in place. Refuses to overwrite an
    existing file at the destination.
    """
    cfg = paths.config_dir()
    data = paths.data_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, str]] = []

    for src in LEGACY_PATHS:
        if not _is_legacy_candidate(src):
            continue
        name = src.name
        if name in _CONFIG_FILES:
            dst = cfg / name
        elif name in _DATA_FILES:
            dst = data / name
        else:
            continue

        if dst.exists():
            skipped.append((src, f"destination {dst} already exists"))
            continue

        try:
            shutil.copy2(src, dst)
            # Match strict perms on the secret key.
            if name == paths.SECRET_KEY_NAME:
                os.chmod(dst, 0o600)
            moved.append((src, dst))
        except OSError as exc:
            skipped.append((src, str(exc)))

    if not moved and not skipped:
        print("No legacy state files found. Nothing to migrate.")
        return 0

    for src, dst in moved:
        print(f"  copied {src} -> {dst}")
    for src, reason in skipped:
        print(f"  skipped {src} ({reason})")

    if moved:
        print("\nDone. Source files were left in place. Verify the daemon picks up the new")
        print("locations, then remove the originals manually if you no longer need them.")
    return 0
