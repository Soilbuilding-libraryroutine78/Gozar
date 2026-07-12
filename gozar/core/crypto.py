"""Envelope encryption for credential material at rest.

Gozar stores all upstream credential secrets (subscription token bundles and API
keys) encrypted at rest (Requirements 1.2, 2.2, 3.2, 16.2). This module implements
AES-256-GCM **envelope encryption**:

* A fresh 256-bit *data encryption key* (DEK) is generated for **every** record.
* The plaintext is sealed with the DEK using AES-256-GCM and a per-record 96-bit
  nonce, producing the stored ``ciphertext``.
* The DEK itself is then wrapped (encrypted) with the *master key* read from
  configuration using a second, independent AES-256-GCM operation, producing
  ``wrapped_dek``.

Only the wrapped DEK and ciphertext are persisted; the plaintext DEK exists only in
memory for the duration of an operation. The master key is never written to storage
and is sourced exclusively from :mod:`gozar.core.config` (no hardcoded key).

The module **fails closed**: if the master key is absent or invalid, every encrypt
and decrypt call raises :class:`MasterKeyError` rather than degrading to a weaker or
unencrypted mode. Tampered or mismatched ciphertext raises :class:`DecryptionError`
(AES-GCM authentication failure).
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gozar.core.config import Settings, get_settings

# AES-256 requires a 32-byte key; GCM uses a 96-bit (12-byte) nonce.
_KEY_SIZE = 32
_NONCE_SIZE = 12


class CryptoError(Exception):
    """Base class for all envelope-encryption failures."""


class MasterKeyError(CryptoError):
    """Raised when the master key is absent, malformed, or the wrong length.

    This is the fail-closed signal: without a valid master key, no credential
    material can be encrypted or decrypted.
    """


class DecryptionError(CryptoError):
    """Raised when ciphertext (or a wrapped DEK) cannot be authenticated.

    Indicates a wrong master key, corrupted storage, or tampering. The underlying
    AES-GCM authentication tag did not verify.
    """


class EncryptedRecord(NamedTuple):
    """The stored components of one envelope-encrypted secret.

    Behaves as the tuple ``(ciphertext, nonce, wrapped_dek)`` (so callers may unpack
    it positionally) while also exposing named fields for clarity. ``wrapped_dek``
    carries its own wrapping nonce as a prefix, so the three values are all that is
    needed to recover the plaintext given the master key.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes


def _load_master_key(settings: Settings | None = None) -> bytes:
    """Load and validate the 32-byte master key from configuration.

    The master key is provided as a base64-encoded 32-byte value via
    ``GOZAR_MASTER_KEY``. Fails closed with :class:`MasterKeyError` if the value is
    missing, not valid base64, or not exactly 32 bytes after decoding.
    """
    settings = settings or get_settings()
    raw = settings.master_key
    if not raw:
        raise MasterKeyError(
            "master key is not configured (set GOZAR_MASTER_KEY to a "
            "base64-encoded 32-byte value)"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError("master key is not valid base64") from exc
    if len(key) != _KEY_SIZE:
        raise MasterKeyError(
            f"master key must decode to {_KEY_SIZE} bytes, got {len(key)}"
        )
    return key


def ensure_master_key(settings: Settings | None = None) -> None:
    """Validate that a usable master key is configured; raise if not (fail closed).

    Intended for startup validation: a deployment without a present, well-formed,
    correctly-sized master key cannot encrypt or decrypt credential material, so it
    must refuse to start rather than run in a degraded state (Requirement 19.3).
    Raises :class:`MasterKeyError` when the key is absent, not valid base64, or not
    exactly 32 bytes after decoding.
    """
    _load_master_key(settings)


def encrypt(
    plaintext: bytes,
    *,
    aad: bytes | None = None,
    settings: Settings | None = None,
) -> EncryptedRecord:
    """Envelope-encrypt ``plaintext`` and return its stored components.

    A fresh random DEK and two independent random nonces are generated per call, so
    encrypting the same plaintext twice yields different ciphertext. The returned
    ``ciphertext`` is the AES-256-GCM sealing of ``plaintext`` under the DEK; it is
    never equal to ``plaintext`` (GCM appends a 16-byte authentication tag).

    Args:
        plaintext: The secret bytes to protect (e.g. a token bundle or API key).
        aad: Optional additional authenticated data bound to the ciphertext; the
            identical value must be supplied to :func:`decrypt`.
        settings: Optional settings override (primarily for testing).

    Returns:
        An :class:`EncryptedRecord` of ``(ciphertext, nonce, wrapped_dek)``.

    Raises:
        MasterKeyError: If the master key is absent or invalid (fail closed).
    """
    master_key = _load_master_key(settings)

    # Per-record data encryption key and nonces.
    dek = AESGCM.generate_key(bit_length=_KEY_SIZE * 8)
    data_nonce = os.urandom(_NONCE_SIZE)
    wrap_nonce = os.urandom(_NONCE_SIZE)

    # Seal the plaintext with the DEK.
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, aad)

    # Wrap (encrypt) the DEK with the master key. The wrapping nonce is prefixed to
    # the wrapped key so the single ``wrapped_dek`` value is self-describing.
    wrapped = AESGCM(master_key).encrypt(wrap_nonce, dek, None)
    wrapped_dek = wrap_nonce + wrapped

    return EncryptedRecord(ciphertext=ciphertext, nonce=data_nonce, wrapped_dek=wrapped_dek)


def decrypt(
    ciphertext: bytes,
    nonce: bytes,
    wrapped_dek: bytes,
    *,
    aad: bytes | None = None,
    settings: Settings | None = None,
) -> bytes:
    """Recover the original plaintext from envelope-encrypted components.

    Unwraps the DEK with the master key, then decrypts ``ciphertext`` under the DEK.
    The ``aad`` must match the value used at encryption time.

    Args:
        ciphertext: The sealed secret produced by :func:`encrypt`.
        nonce: The per-record data nonce returned by :func:`encrypt`.
        wrapped_dek: The wrapping-nonce-prefixed wrapped DEK from :func:`encrypt`.
        aad: Optional additional authenticated data used at encryption time.
        settings: Optional settings override (primarily for testing).

    Returns:
        The original plaintext bytes.

    Raises:
        MasterKeyError: If the master key is absent or invalid (fail closed).
        DecryptionError: If authentication fails (wrong key, corruption, tampering,
            or a malformed wrapped DEK).
    """
    master_key = _load_master_key(settings)

    if len(wrapped_dek) <= _NONCE_SIZE:
        raise DecryptionError("wrapped DEK is malformed or truncated")
    wrap_nonce, wrapped = wrapped_dek[:_NONCE_SIZE], wrapped_dek[_NONCE_SIZE:]

    try:
        dek = AESGCM(master_key).decrypt(wrap_nonce, wrapped, None)
    except InvalidTag as exc:
        raise DecryptionError(
            "failed to unwrap data key (wrong master key or corrupted record)"
        ) from exc

    try:
        return AESGCM(dek).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "failed to decrypt ciphertext (corrupted record or wrong nonce/aad)"
        ) from exc
