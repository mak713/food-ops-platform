"""Phase 11 Settings API (AUTH-007, SET-001..005, §8.4, §8.33): GET/PATCH shape, partial
update semantics, validation envelopes, CSRF/auth, owner profile, signup timezone hardening."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text

from app.db.models.business import Business
from app.db.models.user import User
from tests.integration.api.helpers import (
    ORIGIN_HEADERS,
    csrf_headers,
    signup,
    signup_payload,
)

BASE = "/api/v1/settings"


def _get(client) -> dict:
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client, version: int, **fields):
    return client.patch(
        f"{BASE}/business", json={"version": version, **fields}, headers=csrf_headers(client)
    )


def _error_fields(response) -> set[str]:
    return {issue.get("field") for issue in response.json()["error"]["issues"]}


# ------------------------------------------------------------------------------------ GET


def test_get_returns_seeded_defaults_and_the_documented_shape(client):
    me = signup(client, business_name="Sweet Co", business_timezone="America/Chicago")
    body = _get(client)
    assert body == {
        "id": me["business"]["id"],
        "name": "Sweet Co",
        "business_type": None,
        "contact_phone": None,
        "contact_email": None,
        "address_text": None,
        "timezone": "America/Chicago",
        "default_labor_rate": "0.00",
        "target_contribution_margin": None,
        "fulfillment_warning_minutes": 60,
        "shopping_horizon_days": 7,
        "version": 1,
    }
    # default_fulfillment_method is out of Phase 11 scope: never exposed.
    assert "default_fulfillment_method" not in body


def test_get_requires_authentication(client):
    response = client.get(BASE)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_UNAUTHENTICATED"


# ----------------------------------------------------------------------------------- PATCH


def test_patch_persists_every_editable_group_and_round_trips(client, session):
    signup(client)
    body = _patch(
        client,
        1,
        name="  Renamed Bakery  ",
        business_type="Home bakery",
        contact_phone="555-0100",
        contact_email="Hello@Example.com",
        address_text="1 Main St\nSuite 2",
        timezone="Asia/Kolkata",
        default_labor_rate="18.50",
        target_contribution_margin="0.625",
        fulfillment_warning_minutes=45,
        shopping_horizon_days=3,
    )
    assert body.status_code == 200, body.text
    saved = body.json()
    assert saved["name"] == "Renamed Bakery"
    assert saved["contact_email"] == "Hello@Example.com"
    assert saved["default_labor_rate"] == "18.50"
    assert saved["target_contribution_margin"] == "0.625000"
    assert saved["timezone"] == "Asia/Kolkata"
    assert saved["fulfillment_warning_minutes"] == 45
    assert saved["shopping_horizon_days"] == 3
    assert saved["version"] == 2
    assert _get(client) == saved

    stored = session.execute(
        text("select target_contribution_margin::text, default_labor_rate::text from businesses")
    ).one()
    assert stored == ("0.625000", "18.50")


def test_partial_update_leaves_other_fields_unchanged(client):
    signup(client, business_name="Keep Me")
    first = _patch(client, 1, default_labor_rate="12.00", contact_phone="123").json()
    second = _patch(client, first["version"], shopping_horizon_days=3).json()
    assert second["name"] == "Keep Me"
    assert second["default_labor_rate"] == "12.00"
    assert second["contact_phone"] == "123"
    assert second["shopping_horizon_days"] == 3


def test_explicit_null_clears_nullable_fields_and_blank_becomes_null(client):
    signup(client)
    v = _patch(client, 1, business_type="X", target_contribution_margin="0.4").json()["version"]
    cleared = _patch(client, v, business_type="   ", target_contribution_margin=None).json()
    assert cleared["business_type"] is None
    assert cleared["target_contribution_margin"] is None


def test_version_increments_only_on_a_real_change(client):
    signup(client)
    unchanged = _patch(client, 1, name="Test Bakery", shopping_horizon_days=7)
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == 1
    empty = _patch(client, 1)
    assert empty.status_code == 200
    assert empty.json()["version"] == 1
    changed = _patch(client, 1, name="Changed")
    assert changed.json()["version"] == 2


def test_a_noop_patch_still_checks_the_version(client):
    signup(client)
    _patch(client, 1, name="Changed")
    stale = _patch(client, 1, name="Changed")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_VERSION"


def test_stale_version_returns_409_with_current_version(client):
    signup(client)
    _patch(client, 1, name="First")
    stale = _patch(client, 1, name="Second")
    assert stale.status_code == 409
    issue = stale.json()["error"]["issues"][0]
    assert issue["code"] == "STALE_VERSION"
    assert issue["resource"] == "business"
    assert issue["details"] == {"current_version": 2}
    assert _get(client)["name"] == "First"


def test_default_fulfillment_method_is_never_exposed_or_changed(client, session):
    signup(client)
    session.execute(text("update businesses set default_fulfillment_method = 'DELIVERY'"))
    session.commit()
    saved = _patch(client, 1, name="Other", default_fulfillment_method="PICKUP").json()
    assert "default_fulfillment_method" not in saved
    assert (
        session.execute(text("select default_fulfillment_method::text from businesses")).scalar()
        == "DELIVERY"
    )


def test_validation_errors_use_the_structured_envelope(client):
    signup(client)
    cases = {
        "timezone": {"timezone": "Mars/Phobos"},
        "default_labor_rate": {"default_labor_rate": "-1"},
        "target_contribution_margin": {"target_contribution_margin": "1"},
        "fulfillment_warning_minutes": {"fulfillment_warning_minutes": 0},
        "shopping_horizon_days": {"shopping_horizon_days": 5},
        "contact_email": {"contact_email": "nope"},
        "name": {"name": "   "},
    }
    for field, payload in cases.items():
        response = _patch(client, 1, **payload)
        assert response.status_code == 422, (field, response.text)
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert field in _error_fields(response)
    assert _get(client)["version"] == 1


def test_required_fields_reject_explicit_null(client):
    signup(client)
    for field in ("name", "timezone", "default_labor_rate", "fulfillment_warning_minutes"):
        response = _patch(client, 1, **{field: None})
        assert response.status_code == 422
        assert field in _error_fields(response)


def test_warning_minutes_accepts_the_postgres_integer_maximum_and_no_lower_product_cap(client):
    signup(client)
    big = _patch(client, 1, fulfillment_warning_minutes=2_147_483_647)
    assert big.status_code == 200, big.text
    assert big.json()["fulfillment_warning_minutes"] == 2_147_483_647
    too_big = _patch(client, 2, fulfillment_warning_minutes=2_147_483_648)
    assert too_big.status_code == 422


def test_margin_is_stored_as_an_exact_ratio(client, session):
    signup(client)
    _patch(client, 1, target_contribution_margin="0.625000")
    stored = session.execute(select(Business.target_contribution_margin)).scalar_one()
    assert stored == Decimal("0.625000")
    assert isinstance(stored, Decimal)


def test_patch_requires_csrf_and_authentication(client):
    unauthenticated = client.patch(
        f"{BASE}/business", json={"version": 1, "name": "x"}, headers=ORIGIN_HEADERS
    )
    assert unauthenticated.status_code == 401

    signup(client)
    missing_csrf = client.patch(
        f"{BASE}/business", json={"version": 1, "name": "x"}, headers=ORIGIN_HEADERS
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "AUTH_CSRF_MISMATCH"
    bad_origin = client.patch(
        f"{BASE}/business",
        json={"version": 1, "name": "x"},
        headers={**csrf_headers(client), "Origin": "https://evil.example"},
    )
    assert bad_origin.status_code == 403
    owner = client.patch(f"{BASE}/owner", json={"name": "x"}, headers=ORIGIN_HEADERS)
    assert owner.status_code == 403
    assert _get(client)["name"] == "Test Bakery"


def test_me_reflects_a_changed_business_name_and_timezone(client):
    signup(client)
    _patch(client, 1, name="After", timezone="Europe/London")
    me = client.get("/api/v1/auth/me").json()
    assert me["business"]["name"] == "After"
    assert me["business"]["timezone"] == "Europe/London"


# ------------------------------------------------------------------------------- owner


def test_owner_name_update_persists_and_me_reflects_it_at_once(client, session):
    me = signup(client, name="Old Name")
    response = client.patch(
        f"{BASE}/owner", json={"name": "  New Name  "}, headers=csrf_headers(client)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": me["user"]["id"],
        "name": "New Name",
        "email": me["user"]["email"],
    }
    assert client.get("/api/v1/auth/me").json()["user"]["name"] == "New Name"
    assert session.execute(select(User.name)).scalar_one() == "New Name"


def test_owner_update_cannot_change_email_or_password(client, session):
    payload = signup_payload()
    client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    before = session.execute(select(User.email, User.password_hash)).one()
    response = client.patch(
        f"{BASE}/owner",
        json={"name": "Renamed", "email": "attacker@example.com", "password_hash": "x"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    session.expire_all()
    after = session.execute(select(User.email, User.password_hash)).one()
    assert after == before
    # still able to log in with the original credentials
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
        headers=ORIGIN_HEADERS,
    )
    assert login.status_code == 200


def test_owner_name_validation(client):
    signup(client)
    for bad in ("", "   ", "x" * 201):
        response = client.patch(f"{BASE}/owner", json={"name": bad}, headers=csrf_headers(client))
        assert response.status_code == 422


def test_owner_update_does_not_touch_business_version(client):
    signup(client)
    client.patch(f"{BASE}/owner", json={"name": "New"}, headers=csrf_headers(client))
    assert _get(client)["version"] == 1


# ------------------------------------------------------------------- signup hardening


def test_signup_rejects_an_invalid_timezone_with_a_field_level_422(client, session):
    payload = signup_payload(business_timezone="Mars/Phobos")
    response = client.post("/api/v1/auth/signup", json=payload, headers=ORIGIN_HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "business_timezone" in _error_fields(response)
    assert session.execute(select(User).where(User.email == payload["email"])).first() is None


def test_signup_still_accepts_valid_and_legacy_alias_timezones(client):
    for zone in ("America/New_York", "UTC", "US/Pacific", "Asia/Kolkata"):
        client.cookies.clear()
        me = signup(client, business_timezone=zone)
        assert me["business"]["timezone"] == zone
