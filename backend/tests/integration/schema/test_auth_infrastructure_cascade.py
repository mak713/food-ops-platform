"""Phase 2 auth-infrastructure ON DELETE behavior (Phase 2 plan §9, §16).

`sessions` and `password_reset_tokens` FK to `users.id` with `ON DELETE CASCADE` — the
opposite direction from the `businesses`<->`users` `NO ACTION` pair covered by Phase 1's
`test_cascade_behavior.py::test_users_business_deletion_protection`. That existing test is
left untouched; this file adds the new, Phase-2-specific coverage separately, following the
same conventions (real Postgres, `text()` row-count assertions, factories module).
"""

from sqlalchemy import text

from tests.integration.schema import factories as f


def test_user_deletion_cascades_to_sessions_and_password_reset_tokens(session):
    """Deleting a User (after their Business is already gone, since that direction is
    still NO ACTION per Phase 1) must cleanly remove their session/reset-token rows
    without any separate application-level cleanup step — this is what makes account
    deletion (Phase 2 plan §7) atomic without a manual session-revocation step."""
    user = f.make_user(session)
    business = f.make_business(session, user)
    session.flush()
    auth_session = f.make_auth_session(session, user)
    reset_token = f.make_password_reset_token(session, user)
    session.flush()
    session_id = auth_session.id
    reset_token_id = reset_token.id

    session.execute(text("DELETE FROM businesses WHERE id = :id"), {"id": business.id})
    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    session.flush()

    remaining_sessions = session.execute(
        text("SELECT COUNT(*) FROM sessions WHERE id = :id"), {"id": session_id}
    ).scalar_one()
    remaining_reset_tokens = session.execute(
        text("SELECT COUNT(*) FROM password_reset_tokens WHERE id = :id"), {"id": reset_token_id}
    ).scalar_one()
    assert remaining_sessions == 0
    assert remaining_reset_tokens == 0
