"""Authentication primitives: password hashing, opaque token generation/hashing, and the
constant-time comparisons CSRF validation depends on.

No application signing/entropy secret is used anywhere in this module (Phase 2 plan §4):
every token is high-entropy random (`secrets.token_urlsafe`) and only ever compared/stored
via a SHA-256 hash, so there is nothing to sign and nothing that needs a server-held secret
key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# --- Cookie / header names -------------------------------------------------------------

SESSION_COOKIE_NAME = "fo_session"
CSRF_COOKIE_NAME = "fo_csrf"
CSRF_HEADER_NAME = "x-csrf-token"

# --- Session / token lifetimes (Phase 2 plan §5, §6) ------------------------------------

SESSION_IDLE_TTL = timedelta(days=7)
SESSION_ABSOLUTE_TTL = timedelta(days=30)
PASSWORD_RESET_TOKEN_TTL = timedelta(minutes=30)

# --- Password policy (Phase 2 plan §10, approved) ---------------------------------------
#
# Minimum 15 / maximum 128 characters, no composition-class rules, no forced rotation,
# passphrases/spaces/printable characters allowed. Enforced once, here, and imported by
# the Pydantic request schemas that are the actual point of validation — so the numbers
# live in exactly one place. The password value itself is never trimmed or truncated.

PASSWORD_MIN_LENGTH = 15
PASSWORD_MAX_LENGTH = 128

# --- Password hashing (Phase 2 plan §10) -------------------------------------------------
#
# Concrete Argon2id parameters, stated explicitly rather than left on library defaults so
# the choice is documented and stable across argon2-cffi upgrades. Within OWASP's commonly
# cited interactive-login range; tunable without changing any call site.

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,  # iterations
    memory_cost=65536,  # KiB = 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,  # Argon2id
)

# A fixed, precomputed, valid Argon2id hash of a value that is not any real user's
# password. Computed once at import time (not per-request). Used by the login flow to
# perform a real Argon2id verify() even when the submitted email doesn't match any user,
# so "unknown email" and "known email, wrong password" take approximately the same amount
# of work and are not distinguishable by response latency (Phase 2 plan §6a).
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-password-used-for-timing-parity-only")


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id. The password is passed through unmodified —
    never trimmed or truncated (Phase 2 plan §10)."""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored Argon2id hash. Returns False (rather
    than raising) on any mismatch or malformed-hash condition — callers never need to catch
    argon2 exceptions directly."""
    try:
        _PASSWORD_HASHER.verify(stored_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False
    return True


def verify_against_dummy_hash(password: str) -> None:
    """Run a real Argon2id verify() against a fixed dummy hash and discard the result.

    Called by the login flow when the submitted email doesn't match any user, so that path
    performs comparable Argon2id work to the "user exists, password checked" path (Phase 2
    plan §6a). The mismatch is expected and intentionally ignored.
    """
    try:
        _PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        pass


def needs_rehash(stored_hash: str) -> bool:
    """True if a previously-verified hash was produced with weaker-than-current parameters
    and should be upgraded. Callers only invoke this after a *successful* verify_password(),
    when the plaintext is available in-hand to re-hash (Phase 2 plan §10 upgrade path)."""
    return _PASSWORD_HASHER.check_needs_rehash(stored_hash)


# --- Opaque tokens (sessions, CSRF, password resets) -------------------------------------


def generate_token() -> str:
    """A high-entropy, URL-safe opaque token — used as-is for session cookies, CSRF
    cookies, and password-reset tokens. Never signed; only ever compared via its hash."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of an opaque token, for at-rest storage. Fast hashing is
    appropriate here (unlike passwords) because the input is already high-entropy random,
    not a low-entropy user-chosen secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison, used for the CSRF cookie/header double-submit check."""
    return hmac.compare_digest(a, b)
