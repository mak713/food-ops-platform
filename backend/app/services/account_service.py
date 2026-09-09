"""Full account/business deletion (AUTH-008).

Only `users` and `businesses` exist as of Phase 2, and Phase 1's own
`test_full_tenant_graph_deletion_succeeds` already proves `DELETE FROM businesses` cascades
cleanly — so this is real, working deletion now, not groundwork for later (Phase 2 plan §7).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import security
from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.auth import DeleteAccountRequest


def delete_account(
    db: Session, user: User, business: Business, payload: DeleteAccountRequest
) -> None:
    if not security.verify_password(payload.current_password, user.password_hash):
        raise ApiError(401, "AUTH_INVALID_CREDENTIALS", "Current password is incorrect.")

    if payload.business_name_confirmation != business.name:
        raise ApiError(
            422,
            "ACCOUNT_DELETE_CONFIRMATION_MISMATCH",
            "The business name you entered does not match.",
        )

    # Business first, then User — matching the NO ACTION FK direction Phase 1 established
    # (a User can't be deleted while their Business still exists). The new sessions/
    # password_reset_tokens tables FK to users.id with ON DELETE CASCADE, so deleting the
    # User cleans those up automatically; no separate session-revocation step is needed.
    db.delete(business)
    db.flush()
    db.delete(user)
    db.commit()
