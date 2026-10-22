"""Phase 11 Business optimistic concurrency, exercised through the real service layer on real
connections (Spec §8.33/§17.8; ADR-100).

Two racing `update_business_settings` calls both read the same version; each thread's
commit is forced to rendezvous so both UPDATEs are issued against the same loaded version.
Exactly one must win; the loser must surface the structured `STALE_VERSION` ApiError via the
ORM `version_id_col` backstop (`commit_or_raise_stale`) — never a raw `StaleDataError` — and
no update may be lost. Owner-name edits are intentionally last-write-wins (no conflict).
"""

from __future__ import annotations

import threading

from sqlalchemy import select

from app.core.api_errors import ApiError
from app.db.models.business import Business
from app.db.models.user import User
from app.schemas.settings import BusinessSettingsUpdateRequest, OwnerProfileUpdateRequest
from app.services import settings_service
from tests.integration.schema.factories import make_business_graph


def _cleanup(real_session_factory, *, business_id, owner_id):
    db = real_session_factory()
    try:
        business = db.get(Business, business_id)
        if business is not None:
            db.delete(business)
            db.flush()
        user = db.get(User, owner_id)
        if user is not None:
            db.delete(user)
        db.commit()
    finally:
        db.close()


def _make_business(real_session_factory):
    db = real_session_factory()
    try:
        business = make_business_graph(db)
        db.commit()
        return business.id, business.owner_user_id
    finally:
        db.close()


def _race(real_session_factory, run, *, synchronize_commit: bool):
    results: list[tuple[str, object]] = []
    barrier = threading.Barrier(2)

    def attempt(index: int) -> None:
        db = real_session_factory()
        original_commit = db.commit
        if synchronize_commit:

            def synchronized_commit():
                barrier.wait(timeout=5)
                return original_commit()

            db.commit = synchronized_commit  # instance-level only
        try:
            run(db, index)
            results.append(("ok", index))
        except ApiError as exc:
            db.rollback()
            results.append(("api_error", exc))
        except Exception as exc:  # noqa: BLE001 - captured for assertion
            db.rollback()
            results.append(("error", exc))
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results


def test_concurrent_business_settings_updates_have_exactly_one_winner(real_session_factory):
    business_id, owner_id = _make_business(real_session_factory)
    try:

        def run(db, index):
            business = db.get(Business, business_id)
            payload = BusinessSettingsUpdateRequest(version=1, name=f"Writer {index}")
            settings_service.update_business_settings(db, business, payload)

        results = _race(real_session_factory, run, synchronize_commit=True)

        assert len(results) == 2
        kinds = sorted(kind for kind, _ in results)
        assert kinds == ["api_error", "ok"], results
        loser = next(value for kind, value in results if kind == "api_error")
        assert loser.status_code == 409
        assert loser.code == "STALE_VERSION"
        assert loser.issues[0]["resource"] == "business"
        winner_index = next(value for kind, value in results if kind == "ok")

        check = real_session_factory()
        try:
            stored = check.execute(
                select(Business.name, Business.version).where(Business.id == business_id)
            ).one()
        finally:
            check.close()
        assert stored == (f"Writer {winner_index}", 2)
    finally:
        _cleanup(real_session_factory, business_id=business_id, owner_id=owner_id)


def test_concurrent_updates_to_different_fields_still_cannot_both_apply_at_one_version(
    real_session_factory,
):
    """Two writers editing *different* fields from the same loaded version must still
    conflict (whole-record versioning): the loser has to refresh and re-apply."""
    business_id, owner_id = _make_business(real_session_factory)
    try:

        def run(db, index):
            business = db.get(Business, business_id)
            if index == 0:
                payload = BusinessSettingsUpdateRequest(version=1, shopping_horizon_days=3)
            else:
                payload = BusinessSettingsUpdateRequest(version=1, fulfillment_warning_minutes=15)
            settings_service.update_business_settings(db, business, payload)

        results = _race(real_session_factory, run, synchronize_commit=True)
        assert sorted(kind for kind, _ in results) == ["api_error", "ok"]

        check = real_session_factory()
        try:
            row = check.execute(
                select(
                    Business.shopping_horizon_days,
                    Business.fulfillment_warning_minutes,
                    Business.version,
                ).where(Business.id == business_id)
            ).one()
        finally:
            check.close()
        # exactly one of the two edits is present
        applied = (row.shopping_horizon_days == 3) != (row.fulfillment_warning_minutes == 15)
        assert applied, row
        assert row.version == 2
    finally:
        _cleanup(real_session_factory, business_id=business_id, owner_id=owner_id)


def test_loser_can_refresh_and_reapply_successfully(real_session_factory):
    business_id, owner_id = _make_business(real_session_factory)
    try:
        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            settings_service.update_business_settings(
                db, business, BusinessSettingsUpdateRequest(version=1, name="First")
            )
        finally:
            db.close()

        db = real_session_factory()
        try:
            business = db.get(Business, business_id)
            try:
                settings_service.update_business_settings(
                    db, business, BusinessSettingsUpdateRequest(version=1, name="Late")
                )
                raise AssertionError("expected STALE_VERSION")
            except ApiError as exc:
                assert exc.code == "STALE_VERSION"
                assert exc.issues[0]["details"] == {"current_version": 2}
            db.rollback()
            business = db.get(Business, business_id)
            result = settings_service.update_business_settings(
                db, business, BusinessSettingsUpdateRequest(version=business.version, name="Late")
            )
            assert (result.name, result.version) == ("Late", 3)
        finally:
            db.close()
    finally:
        _cleanup(real_session_factory, business_id=business_id, owner_id=owner_id)


def test_owner_name_edits_are_last_write_wins_and_never_conflict(real_session_factory):
    business_id, owner_id = _make_business(real_session_factory)
    try:

        def run(db, index):
            user = db.get(User, owner_id)
            settings_service.update_owner_profile(
                db, user, OwnerProfileUpdateRequest(name=f"Owner {index}")
            )

        results = _race(real_session_factory, run, synchronize_commit=True)
        assert sorted(kind for kind, _ in results) == ["ok", "ok"], results

        check = real_session_factory()
        try:
            name = check.execute(select(User.name).where(User.id == owner_id)).scalar_one()
        finally:
            check.close()
        assert name in {"Owner 0", "Owner 1"}
    finally:
        _cleanup(real_session_factory, business_id=business_id, owner_id=owner_id)
