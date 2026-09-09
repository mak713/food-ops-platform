"""Request/response schemas for the authentication and account-deletion endpoints.

Password-length policy (15-128 characters, no composition rules, never truncated — Phase 2
plan §10) is enforced here, once, via `Field(min_length=..., max_length=...)` sourced from
`app.core.security`'s constants — Pydantic rejects out-of-range values with a clear `422`
rather than silently truncating.

Email format/normalization is handled with a plain validator rather than pulling in the
`email-validator` package: a lightweight regex is enough for V1 shape-checking, and the
existing stack (Pydantic `str` + a validator) already solves the problem without a new
dependency.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


class _EmailField(BaseModel):
    email: str = Field(min_length=1, max_length=254)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class _PasswordField(BaseModel):
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class SignupRequest(_EmailField, _PasswordField):
    name: str = Field(min_length=1, max_length=200)
    business_name: str = Field(min_length=1, max_length=200)
    business_timezone: str = Field(min_length=1, max_length=100)


class LoginRequest(_EmailField):
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str


class BusinessSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    timezone: str


class MeResponse(BaseModel):
    user: UserSummary
    business: BusinessSummary


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class RequestPasswordResetRequest(_EmailField):
    pass


class ConfirmPasswordResetRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class DeleteAccountRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    business_name_confirmation: str = Field(min_length=1, max_length=200)
