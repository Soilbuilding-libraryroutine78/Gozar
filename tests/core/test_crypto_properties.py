"""Property-based tests for envelope encryption of credential material.

These tests validate Property 1 from the Gozar design: encrypting any upstream
credential secret (subscription token bundle or API key) and then decrypting it
returns the original value, and the stored ciphertext is never equal to the
plaintext.
"""

from __future__ import annotations

import base64
import os

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from gozar.core.config import Settings
from gozar.core.crypto import decrypt, encrypt


def _test_settings() -> Settings:
    """Return Settings carrying a valid, randomly generated 32-byte master key.

    Passing this object via the ``settings`` override keeps the test fully isolated
    from the process environment and the cached settings singleton, so crypto does
    not fail closed for lack of a configured master key.
    """
    master_key = base64.b64encode(os.urandom(32)).decode("ascii")
    return Settings(master_key=master_key)


# A single master key is reused across all generated examples; the envelope scheme
# still generates a fresh DEK and nonces per call, so this exercises real behavior.
_SETTINGS = _test_settings()

# Arbitrary bytes, including empty and large payloads, to mirror real credential
# secrets (API keys, JSON token bundles) of unpredictable size and content.
_secret_bytes = st.binary(min_size=0, max_size=4096)


# Feature: gozar, Property 1: Credential encryption round-trip and confidentiality
@hyp_settings(max_examples=200)
@given(plaintext=_secret_bytes)
def test_encryption_round_trip_and_confidentiality(plaintext: bytes) -> None:
    """Validates: Requirements 1.2, 2.2, 3.2, 16.2.

    For any credential secret, decrypt(encrypt(p)) == p (round-trip), and the stored
    ciphertext is never equal to the plaintext (confidentiality).
    """
    record = encrypt(plaintext, settings=_SETTINGS)

    # Confidentiality: the stored ciphertext must never equal the plaintext.
    assert record.ciphertext != plaintext

    # Round-trip: decrypting the stored components recovers the original value.
    recovered = decrypt(
        record.ciphertext,
        record.nonce,
        record.wrapped_dek,
        settings=_SETTINGS,
    )
    assert recovered == plaintext
