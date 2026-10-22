"""Business Settings and owner-profile use cases (Phase 11; Spec AUTH-007, SET-001..005).

Settings are *current* configuration, not history: this module only writes the
`businesses` / `users` columns the owner edits. It performs no recalculation and never
touches Production Runs, `OrderCostAllocation`, completed Orders, ledgers or any snapshot,
so changing the labor rate or timezone cannot rewrite completed historical truth
(SET-005 / ANA-011); future Production Starts and later legitimate recalculation events
simply read the new current value.

Business edits use the project's optimistic-concurrency pattern (`check_version` +
`commit_or_raise_stale`, Spec §8.33). The owner-name edit is intentionally last-write-wins
(Phase 11 Revision 2): `User` carries no `version_id_col`, because wiring it would
silently change login-rehash / password-change / reset concurrency behavior.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.tenant import check_version, commit_or_raise_stale
from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.auth import UserSummary
from app.schemas.settings import (
    BusinessSettingsResponse,
    BusinessSettingsUpdateRequest,
    OwnerProfileUpdateRequest,
)


def to_settings_response(business: Business) -> BusinessSettingsResponse:
    return BusinessSettingsResponse(
        id=str(business.id),
        name=business.name,
        business_type=business.business_type,
        contact_phone=business.contact_phone,
        contact_email=business.contact_email,
        address_text=business.address_text,
        timezone=business.timezone,
        default_labor_rate=business.default_labor_rate,
        target_contribution_margin=business.target_contribution_margin,
        fulfillment_warning_minutes=business.fulfillment_warning_minutes,
        shopping_horizon_days=business.shopping_horizon_days,
        version=business.version,
    )


def update_business_settings(
    db: Session, business: Business, payload: BusinessSettingsUpdateRequest
) -> BusinessSettingsResponse:
    """`business` is the session-derived Business (never client-selected). The submitted
    version is checked first — even for an update that would change nothing — so the
    client's view of the record must always be current (ADR-100)."""
    check_version(business, payload.version, resource="business")

    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    changed = False
    for field, value in changes.items():
        if getattr(business, field) != value:
            setattr(business, field, value)
            changed = True
    if changed:
        commit_or_raise_stale(db, resource="business")
        db.refresh(business)
    return to_settings_response(business)


def update_owner_profile(
    db: Session, user: User, payload: OwnerProfileUpdateRequest
) -> UserSummary:
    """Last-write-wins display-name edit for the authenticated owner only. Email and
    password are not editable here."""
    if user.name != payload.name:
        user.name = payload.name
        db.commit()
        db.refresh(user)
    return UserSummary(id=str(user.id), name=user.name, email=user.email)
