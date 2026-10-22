"""Phase 11 account-deletion contention translation (AUTH-008; Revision 2 Q7).

Only lock contention is translated into the structured, retryable `409
ACCOUNT_DELETE_CONFLICT`: PostgreSQL `55P03` (lock not available — here a *real*
`lock_timeout` firing against a row another transaction holds), `40P01` (deadlock detected),
and the ORM `StaleDataError` that Business optimistic versioning newly introduces on DELETE
(a Settings write committed first). Every other database error must propagate unchanged, and
every translated failure must leave the tenant completely intact (full rollback) so a retry
succeeds.
"""

from __future__ import annotations

import uuid

import psycopg.errors
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core import security
from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.schemas.auth import DeleteAccountRequest
from app.schemas.settings import BusinessSettingsUpdateRequest
from app.services import account_service, settings_service
from tests.integration.api.helpers import ORIGIN_HEADERS, csrf_headers, signup_payload
from tests.integration.schema.factories import make_business, make_customer, make_user

PASSWORD = "correct horse battery staple"


def _payload(name: str) -> DeleteAccountRequest:
    return DeleteAccountRequest(current_password=PASSWORD, business_name_confirmation=name)


def _make_tenant(real_session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    db = real_session_factory()
    try:
        user = make_user(
            db,
            email=f"contention-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=security.hash_password(PASSWORD),
        )
        business = make_business(db, user, name="Contended Co")
        db.flush()
        make_customer(db, business, name="Locked Customer")
        db.commit()
        return business.id, user.id
    finally:
        db.close()


def _cleanup(real_session_factory, business_id, user_id) -> None:
    db = real_session_factory()
    try:
        business = db.get(Business, business_id)
        if business is not None:
            db.delete(business)
            db.flush()
        user = db.get(User, user_id)
        if user is not None:
            db.delete(user)
        db.commit()
    finally:
        db.close()


def _exists(real_session_factory, business_id, user_id) -> tuple[bool, bool, int]:
    db = real_session_factory()
    try:
        return (
            db.get(Business, business_id) is not None,
            db.get(User, user_id) is not None,
            db.execute(
                text("select count(*) from customers where business_id = :b"), {"b": business_id}
            ).scalar_one(),
        )
    finally:
        db.close()


@pytest.fixture()
def short_lock_timeout_factory(settings):
    """A separate engine whose connections time out waiting for a lock after 400 ms."""
    engine = create_engine(
        settings.test_database_url, connect_args={"options": "-c lock_timeout=400"}
    )
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_real_lock_timeout_on_a_tenant_row_translates_to_a_retryable_conflict_and_a_retry_works(
    real_session_factory, short_lock_timeout_factory, test_engine
):
    business_id, user_id = _make_tenant(real_session_factory)
    holder = test_engine.connect()
    try:
        holder_tx = holder.begin()
        holder.execute(
            text("select id from customers where business_id = :b for update"), {"b": business_id}
        )

        db = short_lock_timeout_factory()
        try:
            user = db.get(User, user_id)
            business = db.get(Business, business_id)
            with pytest.raises(ApiError) as caught:
                account_service.delete_account(db, user, business, _payload("Contended Co"))
            assert caught.value.status_code == 409
            assert caught.value.code == "ACCOUNT_DELETE_CONFLICT"
        finally:
            db.close()

        # Fully rolled back: tenant, user and the locked child row are all still there.
        holder_tx.rollback()
        assert _exists(real_session_factory, business_id, user_id) == (True, True, 1)

        # The conflict was transient: with the lock gone, the same request now succeeds.
        db = short_lock_timeout_factory()
        try:
            user = db.get(User, user_id)
            business = db.get(Business, business_id)
            account_service.delete_account(db, user, business, _payload("Contended Co"))
        finally:
            db.close()
        assert _exists(real_session_factory, business_id, user_id) == (False, False, 0)
    finally:
        holder.close()
        _cleanup(real_session_factory, business_id, user_id)


def test_http_lock_timeout_returns_the_structured_409_envelope(
    real_session_factory, short_lock_timeout_factory, test_engine
):
    """Over HTTP with real sessions: the response is the structured conflict, not a 500."""

    def override():
        db = short_lock_timeout_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    app.state.limiter.reset()
    holder = test_engine.connect()
    created: tuple[uuid.UUID, uuid.UUID] | None = None
    try:
        with TestClient(app) as client:
            payload = signup_payload(email=f"http-{uuid.uuid4().hex[:8]}@example.com")
            signup = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
            assert signup.status_code == 201, signup.text
            body = signup.json()
            created = (uuid.UUID(body["business"]["id"]), uuid.UUID(body["user"]["id"]))
            setup = real_session_factory()
            try:
                make_customer(setup, setup.get(Business, created[0]), name="Held")
                setup.commit()
            finally:
                setup.close()

            holder_tx = holder.begin()
            holder.execute(
                text("select id from customers where business_id = :b for update"),
                {"b": created[0]},
            )
            response = client.request(
                "DELETE",
                "/api/v1/account",
                json={
                    "current_password": payload["password"],
                    "business_name_confirmation": payload["business_name"],
                },
                headers=csrf_headers(client),
            )
            holder_tx.rollback()
            assert response.status_code == 409, response.text
            error = response.json()["error"]
            assert error["code"] == "ACCOUNT_DELETE_CONFLICT"
            assert "try again" in error["message"]
            assert _exists(real_session_factory, *created)[:2] == (True, True)
            # Session cookies are NOT cleared on a failed deletion: the user is still signed in.
            assert client.get("/api/v1/auth/me").status_code == 200
    finally:
        holder.close()
        app.dependency_overrides.clear()
        if created is not None:
            _cleanup(real_session_factory, *created)


def test_deadlock_is_translated_with_a_full_rollback(real_session_factory, monkeypatch):
    business_id, user_id = _make_tenant(real_session_factory)
    try:
        db = real_session_factory()
        try:
            user = db.get(User, user_id)
            business = db.get(Business, business_id)
            original_flush = Session.flush

            def deadlocking_flush(self, *args, **kwargs):
                if any(isinstance(obj, Business) for obj in self.deleted):
                    raise OperationalError(
                        "DELETE FROM businesses", {}, psycopg.errors.DeadlockDetected("deadlock")
                    )
                return original_flush(self, *args, **kwargs)

            monkeypatch.setattr(Session, "flush", deadlocking_flush)
            with pytest.raises(ApiError) as caught:
                account_service.delete_account(db, user, business, _payload("Contended Co"))
            assert caught.value.code == "ACCOUNT_DELETE_CONFLICT"
            assert not db.in_transaction() or not db.deleted  # rolled back
            monkeypatch.setattr(Session, "flush", original_flush)
        finally:
            db.close()
        assert _exists(real_session_factory, business_id, user_id) == (True, True, 1)
    finally:
        _cleanup(real_session_factory, business_id, user_id)


def test_settings_write_racing_the_delete_translates_stale_data_error(real_session_factory):
    """The Business was loaded at version 1; a Settings PATCH commits version 2 before the
    deletion's versioned `DELETE ... WHERE version = 1` runs, so it matches zero rows."""
    business_id, user_id = _make_tenant(real_session_factory)
    try:
        deleting = real_session_factory()
        try:
            user = deleting.get(User, user_id)
            business = deleting.get(Business, business_id)  # loaded at version 1

            writer = real_session_factory()
            try:
                other = writer.get(Business, business_id)
                settings_service.update_business_settings(
                    writer, other, BusinessSettingsUpdateRequest(version=1, name="Contended Co")
                )  # no-op: same name -> no version bump
                settings_service.update_business_settings(
                    writer,
                    other,
                    BusinessSettingsUpdateRequest(version=1, default_labor_rate="9.00"),
                )
            finally:
                writer.close()

            with pytest.raises(ApiError) as caught:
                account_service.delete_account(deleting, user, business, _payload("Contended Co"))
            assert caught.value.status_code == 409
            assert caught.value.code == "ACCOUNT_DELETE_CONFLICT"
        finally:
            deleting.close()
        assert _exists(real_session_factory, business_id, user_id) == (True, True, 1)

        retry = real_session_factory()
        try:
            account_service.delete_account(
                retry,
                retry.get(User, user_id),
                retry.get(Business, business_id),
                _payload("Contended Co"),
            )
        finally:
            retry.close()
        assert _exists(real_session_factory, business_id, user_id) == (False, False, 0)
    finally:
        _cleanup(real_session_factory, business_id, user_id)


@pytest.mark.parametrize(
    "error",
    [
        IntegrityError("DELETE", {}, psycopg.errors.CheckViolation("check")),
        IntegrityError("DELETE", {}, psycopg.errors.ForeignKeyViolation("fk")),
        OperationalError("DELETE", {}, psycopg.errors.QueryCanceled("statement timeout")),
        OperationalError("DELETE", {}, psycopg.errors.SerializationFailure("serialization")),
        OperationalError("DELETE", {}, psycopg.errors.ConnectionFailure("lost connection")),
    ],
    ids=["check", "foreign-key", "query-canceled-57014", "serialization-40001", "connection"],
)
def test_unrelated_database_errors_propagate_unchanged(real_session_factory, monkeypatch, error):
    business_id, user_id = _make_tenant(real_session_factory)
    try:
        db = real_session_factory()
        try:
            user = db.get(User, user_id)
            business = db.get(Business, business_id)
            original_flush = Session.flush

            def failing_flush(self, *args, **kwargs):
                if any(isinstance(obj, Business) for obj in self.deleted):
                    raise error
                return original_flush(self, *args, **kwargs)

            monkeypatch.setattr(Session, "flush", failing_flush)
            with pytest.raises(DBAPIError) as caught:
                account_service.delete_account(db, user, business, _payload("Contended Co"))
            assert caught.value is error  # re-raised as-is, never wrapped or converted to 409
            monkeypatch.setattr(Session, "flush", original_flush)
        finally:
            db.close()
        assert _exists(real_session_factory, business_id, user_id) == (True, True, 1)
    finally:
        _cleanup(real_session_factory, business_id, user_id)
