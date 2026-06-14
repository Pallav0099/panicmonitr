"""Environment helpers for spawning external system executables.

A PyInstaller onefile binary prepends its private unpack directory (the path in
``sys._MEIPASS``) to ``LD_LIBRARY_PATH`` so the bundled CPython loads its own
shared libraries. That variable is inherited by every child process we spawn —
which makes *system* binaries (``systemd-creds``, ``systemctl``, ``/bin/bash``)
load our bundled, often-older ``libcrypto``/``libc`` instead of the host's,
failing with errors like::

    systemd-creds: /tmp/_MEIxxxx/libcrypto.so.3: version `OPENSSL_3.4.0' not found

PyInstaller's bootloader saves the pre-launch value in ``<VAR>_ORIG``. Restore
it (or drop the variable entirely if there was none) before exec'ing anything
that belongs to the host system.
"""
from __future__ import annotations

import os
import sys

# Linux uses LD_LIBRARY_PATH; macOS uses DYLD_LIBRARY_PATH. PyInstaller mangles
# both, so undo both regardless of platform.
_LIB_PATH_VARS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


def system_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``os.environ`` safe for spawning host executables.

    When frozen, the dynamic-linker search path is reset to the value that was
    in effect before the PyInstaller bootloader ran, so the child process loads
    the system's libraries rather than ours. Outside a frozen build the
    environment is returned unchanged. ``extra`` is merged in last.
    """
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        for var in _LIB_PATH_VARS:
            original = env.get(var + "_ORIG")
            if original is not None:
                env[var] = original
            else:
                env.pop(var, None)
    if extra:
        env.update(extra)
    return env
