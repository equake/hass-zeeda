"""Tests for the Zeeda REST API helpers."""

from __future__ import annotations

import pytest

from custom_components.zeeda.api import encrypt_password


@pytest.mark.parametrize(
    ("synthetic_plaintext", "android_ciphertext"),
    (
        (
            "synthetic-only-1",
            "B536FFAC998609D441A6AC381EFFFFCCD35BF5E22D4E76D65CB171216868AAB4",
        ),
        (
            "invented-test-value-2",
            "2E70E8532CB29EC0E28D902EDCA139396933C88C684C12E5AB0E49B3FD741310",
        ),
        ("offline-test-ç", "929CC3C4FA899E06938A3E68CA6F986D"),
    ),
)
def test_encrypt_password_matches_android_ciphertext(
    synthetic_plaintext: str, android_ciphertext: str
) -> None:
    """Vectors use invented inputs, never credentials from a real account."""
    assert encrypt_password(synthetic_plaintext) == android_ciphertext
