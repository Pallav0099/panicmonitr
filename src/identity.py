from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Optional

import nacl.encoding
import nacl.exceptions
import nacl.secret
import nacl.signing
from argon2.low_level import Type, hash_secret_raw
from loguru import logger
from pydantic import BaseModel

from src import paths

SECRET_KEY_LENGTH = 32
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_SALT_LENGTH = 16
MIN_PASSWORD_LENGTH = 12

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


def _default_identity_path() -> Path:
    return paths.default_identity_path()


def _default_meta_path() -> Path:
    return paths.default_meta_path()


# Back-compat aliases. Code that imports these gets a Path resolved at import
# time; for fresh resolution (env overrides), prefer paths.default_*_path().
DEFAULT_IDENTITY_PATH = _default_identity_path()
DEFAULT_META_PATH = _default_meta_path()


def validate_node_id(value: str) -> bool:
    """Return True iff *value* is a 64-char lowercase hex string (an iroh NodeID)."""
    return bool(_HEX_64_RE.match(value))


class IdentityMeta(BaseModel):
    """Public metadata for a sealed ``secret.key``.

    Stored alongside the ciphertext. Contains the argon2id parameters needed
    to derive the KEK and the NodeID (public, so read-only commands like
    ``--list-peers`` don't need a password prompt).
    """

    version: int = 1
    node_id: str
    salt: str  # hex
    time_cost: int = ARGON2_TIME_COST
    memory_cost: int = ARGON2_MEMORY_COST
    parallelism: int = ARGON2_PARALLELISM


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_kek(password: str, salt: bytes, meta: IdentityMeta) -> bytes:
    return hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=meta.time_cost,
        memory_cost=meta.memory_cost,
        parallelism=meta.parallelism,
        hash_len=SECRET_KEY_LENGTH,
        type=Type.ID,
    )


def _derive_node_id(seed: bytes) -> str:
    return (
        nacl.signing.SigningKey(seed)
        .verify_key.encode(encoder=nacl.encoding.HexEncoder)
        .decode()
    )


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    tmp.replace(path)


def _atomic_write_text(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.chmod(tmp, mode)
    tmp.replace(path)


def _seal(seed: bytes, password: str, key_path: Path, meta_path: Path) -> IdentityMeta:
    """argon2id(password, salt) → KEK → SecretBox(seed). Writes key + meta atomically."""
    salt = secrets.token_bytes(ARGON2_SALT_LENGTH)
    meta = IdentityMeta(node_id=_derive_node_id(seed), salt=salt.hex())
    kek = _derive_kek(password, salt, meta)
    ciphertext = nacl.secret.SecretBox(kek).encrypt(seed)
    _atomic_write_bytes(key_path, ciphertext)
    _atomic_write_text(meta_path, meta.model_dump_json(indent=2))
    return meta


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------

def is_sealed(key_path: Path, meta_path: Path) -> bool:
    """True if *key_path* + *meta_path* together form a sealed identity."""
    return key_path.exists() and meta_path.exists()


def is_raw(key_path: Path, meta_path: Path) -> bool:
    """True if *key_path* is a legacy raw-32-byte unsealed seed."""
    if meta_path.exists() or not key_path.exists():
        return False
    try:
        return len(key_path.read_bytes()) == SECRET_KEY_LENGTH
    except OSError:
        return False


def load_meta(meta_path: Path) -> IdentityMeta:
    return IdentityMeta.model_validate_json(meta_path.read_text())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init_sealed_identity(
    password: str,
    key_path: Path = DEFAULT_IDENTITY_PATH,
    meta_path: Path = DEFAULT_META_PATH,
) -> tuple[bytes, IdentityMeta]:
    """Generate a fresh 32-byte seed, seal it under *password*, persist to disk."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    seed = secrets.token_bytes(SECRET_KEY_LENGTH)
    meta = _seal(seed, password, key_path, meta_path)
    logger.info("Sealed identity written -> {}", key_path)
    return seed, meta


def seal_existing_identity(
    password: str,
    key_path: Path = DEFAULT_IDENTITY_PATH,
    meta_path: Path = DEFAULT_META_PATH,
) -> tuple[bytes, IdentityMeta]:
    """Seal an existing raw (unsealed) ``secret.key`` in place with *password*."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    raw = key_path.read_bytes()
    if len(raw) != SECRET_KEY_LENGTH:
        raise ValueError(
            f"secret.key is {len(raw)} bytes -- not an unsealed ed25519 seed"
        )
    meta = _seal(raw, password, key_path, meta_path)
    logger.info("Existing identity sealed in place -> {}", key_path)
    return raw, meta


def unlock_identity(
    password: str,
    key_path: Path = DEFAULT_IDENTITY_PATH,
    meta_path: Path = DEFAULT_META_PATH,
) -> tuple[bytes, IdentityMeta]:
    """Decrypt a sealed identity with *password*. Returns ``(seed, meta)``.

    Raises ``ValueError`` on wrong password or corruption.
    """
    if not key_path.exists():
        raise FileNotFoundError(f"Identity not found at {key_path}")
    if not meta_path.exists():
        raise FileNotFoundError(
            f"{meta_path} missing -- cannot unlock a sealed key without its salt"
        )
    meta = load_meta(meta_path)
    salt = bytes.fromhex(meta.salt)
    kek = _derive_kek(password, salt, meta)
    try:
        seed = nacl.secret.SecretBox(kek).decrypt(key_path.read_bytes())
    except nacl.exceptions.CryptoError:
        raise ValueError("wrong password or corrupted secret.key")
    if len(seed) != SECRET_KEY_LENGTH:
        raise ValueError(
            f"decrypted seed is {len(seed)} bytes (expected {SECRET_KEY_LENGTH})"
        )
    # Sanity check: derived NodeID must match the cleartext meta.node_id.
    derived = _derive_node_id(seed)
    if derived != meta.node_id:
        raise ValueError(
            "decrypted seed does not derive the NodeID recorded in secret.meta "
            "-- secret.key or secret.meta has been tampered with"
        )
    return seed, meta


def reset_password(
    old_password: str,
    new_password: str,
    key_path: Path = DEFAULT_IDENTITY_PATH,
    meta_path: Path = DEFAULT_META_PATH,
) -> IdentityMeta:
    """Re-seal the existing seed under a new password. Key material is unchanged."""
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"New password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    seed, _ = unlock_identity(old_password, key_path, meta_path)
    meta = _seal(seed, new_password, key_path, meta_path)
    logger.info("Identity re-sealed under new password")
    return meta


