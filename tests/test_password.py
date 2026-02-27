"""Tests for swarm.auth.password — hash/verify helpers."""

from __future__ import annotations

from swarm.auth.password import hash_password, is_hashed, verify_password


class TestHashPassword:
    def test_round_trip(self) -> None:
        hashed = hash_password("secret123")
        assert is_hashed(hashed)
        assert verify_password("secret123", hashed)

    def test_different_passwords_produce_different_hashes(self) -> None:
        h1 = hash_password("abc")
        h2 = hash_password("abc")
        # Different salts → different hashes
        assert h1 != h2
        # Both verify correctly
        assert verify_password("abc", h1)
        assert verify_password("abc", h2)

    def test_wrong_password_rejected(self) -> None:
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)

    def test_format(self) -> None:
        hashed = hash_password("test")
        parts = hashed.split(":")
        assert len(parts) == 3
        assert parts[0] == "scrypt"
        # Salt and hash are hex-encoded
        bytes.fromhex(parts[1])
        bytes.fromhex(parts[2])


class TestVerifyPlaintext:
    def test_plaintext_match(self) -> None:
        assert verify_password("mypassword", "mypassword")

    def test_plaintext_mismatch(self) -> None:
        assert not verify_password("wrong", "mypassword")


class TestTokenPassthrough:
    """Dashboard injects the stored hash and sends it back as-is."""

    def test_hash_sent_as_token(self) -> None:
        hashed = hash_password("secret")
        # Sending the hash itself should be accepted (exact match)
        assert verify_password(hashed, hashed)

    def test_wrong_hash_rejected(self) -> None:
        h1 = hash_password("secret1")
        h2 = hash_password("secret2")
        assert not verify_password(h1, h2)


class TestEdgeCases:
    def test_empty_password(self) -> None:
        assert not verify_password("", "stored")

    def test_empty_stored(self) -> None:
        assert not verify_password("pw", "")

    def test_both_empty(self) -> None:
        assert not verify_password("", "")

    def test_malformed_hash(self) -> None:
        assert not verify_password("pw", "scrypt:bad")
        assert not verify_password("pw", "scrypt:not_hex:not_hex")


class TestIsHashed:
    def test_hashed(self) -> None:
        assert is_hashed("scrypt:aabb:ccdd")

    def test_plaintext(self) -> None:
        assert not is_hashed("plaintext-password")
