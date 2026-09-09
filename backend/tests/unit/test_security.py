"""Pure-function tests for app.core.security — no database, no HTTP. First tests under
tests/unit/, alongside the existing DB-driven tests/integration/ tree (Phase 2 plan §17)."""

import pytest
from pydantic import ValidationError

from app.core import security
from app.schemas.auth import ChangePasswordRequest, SignupRequest


class TestPasswordHashing:
    def test_hash_and_verify_round_trip(self):
        hashed = security.hash_password("correct horse battery staple 42")
        assert security.verify_password("correct horse battery staple 42", hashed)

    def test_verify_rejects_wrong_password(self):
        hashed = security.hash_password("correct horse battery staple 42")
        assert not security.verify_password("wrong password entirely here", hashed)

    def test_verify_rejects_malformed_hash(self):
        assert not security.verify_password("anything at all here", "not-a-real-argon2-hash")

    def test_hash_uses_argon2id(self):
        hashed = security.hash_password("correct horse battery staple 42")
        assert hashed.startswith("$argon2id$")

    def test_needs_rehash_false_for_current_parameters(self):
        hashed = security.hash_password("correct horse battery staple 42")
        assert security.needs_rehash(hashed) is False

    def test_password_is_never_trimmed_or_truncated(self):
        # A leading/trailing space is semantically part of the password (Phase 2 plan
        # §10) — hashing " x " must not verify against "x".
        hashed = security.hash_password(" leading and trailing space password 1")
        assert not security.verify_password("leading and trailing space password 1", hashed)
        assert security.verify_password(" leading and trailing space password 1", hashed)


class TestLoginEnumerationResistance:
    def test_verify_against_dummy_hash_never_raises(self):
        # Should swallow the expected VerifyMismatchError internally regardless of input.
        security.verify_against_dummy_hash("literally anything")
        security.verify_against_dummy_hash("")

    def test_dummy_hash_is_a_real_argon2id_hash(self):
        assert security._DUMMY_PASSWORD_HASH.startswith("$argon2id$")


class TestTokensAndHashing:
    def test_generate_token_is_high_entropy_and_unique(self):
        tokens = {security.generate_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(t) >= 32 for t in tokens)

    def test_hash_token_is_deterministic_sha256_hex(self):
        token = "fixed-example-token-value"
        digest = security.hash_token(token)
        assert digest == security.hash_token(token)
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not valid hex

    def test_hash_token_differs_for_different_tokens(self):
        assert security.hash_token("token-a") != security.hash_token("token-b")

    def test_constant_time_compare(self):
        assert security.constant_time_compare("same-value", "same-value")
        assert not security.constant_time_compare("same-value", "different-value")


class TestPasswordPolicyEnforcement:
    """The policy (15-128 chars, no truncation) is enforced once, at the Pydantic schema
    layer (Phase 2 plan §10) — exercised here via the actual request schemas rather than
    re-testing the constants in isolation."""

    _base_signup_fields = {
        "name": "Test Owner",
        "email": "owner@example.com",
        "business_name": "Test Bakery",
        "business_timezone": "America/New_York",
    }

    def test_14_characters_rejected(self):
        with pytest.raises(ValidationError):
            SignupRequest(password="a" * 14, **self._base_signup_fields)

    def test_15_characters_accepted(self):
        SignupRequest(password="a" * 15, **self._base_signup_fields)

    def test_128_characters_accepted(self):
        SignupRequest(password="a" * 128, **self._base_signup_fields)

    def test_129_characters_rejected_not_truncated(self):
        with pytest.raises(ValidationError):
            SignupRequest(password="a" * 129, **self._base_signup_fields)

    def test_passphrase_with_spaces_accepted(self):
        SignupRequest(password="this is a valid passphrase!", **self._base_signup_fields)

    def test_change_password_new_password_enforces_same_policy(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="whatever-current", new_password="short")
