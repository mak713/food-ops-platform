"""Request/response schemas for the Customer resource (Phase 3 plan v3 §12).

Duplicate-match normalization (Phase 3 plan v3 §11/§12): name is trimmed + case-folded,
email is trimmed + case-folded (reusing `app.schemas.auth.normalize_email` rather than a
new ad hoc validator), phone is trimmed only — no digit-stripping or country-code
assumptions, since that would require a phone-parsing dependency this plan explicitly
avoids for an advisory-only feature.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.auth import normalize_email


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CustomerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=254)
    preferred_contact_method: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    # Duplicate-warning resubmit flag (Spec §17.5's "safe implementation pattern"; Phase 3
    # plan v3 §11) — False by default; the client resubmits with True after the owner
    # acknowledges a POSSIBLE_DUPLICATE_CUSTOMER warning.
    confirm_duplicate: bool = False

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name is required.")
        return stripped

    @field_validator("phone", "preferred_contact_method", "notes")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @field_validator("email")
    @classmethod
    def _normalize_email_optional(cls, value: str | None) -> str | None:
        stripped = _strip_or_none(value)
        if stripped is None:
            return None
        return normalize_email(stripped)


class CustomerUpdateRequest(BaseModel):
    """Partial update — every field besides `version` is optional and, when omitted,
    leaves the existing value unchanged (Phase 3 plan v3 §12). `is_active` is
    deliberately absent: lifecycle transitions go through the dedicated archive/reactivate
    actions only (Spec §11.8 — no generic client-controlled status PATCH)."""

    version: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=254)
    preferred_contact_method: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @field_validator("phone", "preferred_contact_method", "notes")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return _strip_or_none(value)

    @field_validator("email")
    @classmethod
    def _normalize_email_optional(cls, value: str | None) -> str | None:
        stripped = _strip_or_none(value)
        if stripped is None:
            return None
        return normalize_email(stripped)

    # Remediation (Checkpoint 3 review): `name` is typed `str | None` only so the field
    # can be *omitted* (meaning "leave unchanged" — see the service layer's
    # `exclude_unset=True`). An explicit `{"name": null}` is a different thing entirely —
    # `customers.name` is NOT NULL — and must be rejected here with a normal 422, not
    # reach the database as a raw IntegrityError. `model_fields_set` (which
    # `exclude_unset` is itself built on) is exactly "was this field present in the
    # request," so this only fires when the client actually sent `name: null`, never when
    # they simply left it out.
    @model_validator(mode="after")
    def _reject_explicit_null_for_non_nullable_fields(self) -> CustomerUpdateRequest:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be set to null.")
        return self


class LifecycleActionRequest(BaseModel):
    """Body for archive/reactivate actions (Customer, Product, SellingOption) — carries
    only the version the client last read (Phase 3 plan v3 §4)."""

    version: int


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    phone: str | None
    email: str | None
    preferred_contact_method: str | None
    notes: str | None
    is_active: bool
    version: int


class CustomerSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    phone: str | None
    email: str | None
    is_active: bool
    version: int
