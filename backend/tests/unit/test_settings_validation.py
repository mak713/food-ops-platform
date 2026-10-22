"""Phase 11 validation rules: the shared IANA timezone validator and the Settings request
schemas. Only grounded constraints are enforced (no invented length limits)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.timezone import validate_iana_timezone
from app.schemas.auth import SignupRequest
from app.schemas.settings import (
    POSTGRES_INTEGER_MAX,
    BusinessSettingsResponse,
    BusinessSettingsUpdateRequest,
    OwnerProfileUpdateRequest,
)

D = Decimal


def _patch(**fields) -> BusinessSettingsUpdateRequest:
    return BusinessSettingsUpdateRequest(version=1, **fields)


def _error_fields(exc: ValidationError) -> set[str]:
    return {str(e["loc"][0]) for e in exc.errors()}


# ------------------------------------------------------------------------ timezone validator


@pytest.mark.parametrize(
    "value",
    ["UTC", "EST", "US/Pacific", "America/New_York", "Asia/Kolkata", "Europe/London", "  UTC  "],
)
def test_valid_timezones_are_accepted_and_trimmed(value):
    assert validate_iana_timezone(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Mars/Phobos",
        "localtime",
        "../etc/passwd",
        "america/new_york",  # case-exact membership
        "America/New York",
        "UTC\x00",
        "Not A Zone",
    ],
)
def test_invalid_timezones_are_rejected(value):
    with pytest.raises(ValueError):
        validate_iana_timezone(value)


def test_signup_uses_the_shared_validator():
    base = {
        "name": "Owner",
        "email": "o@example.com",
        "password": "correct horse battery staple",
        "business_name": "Bakery",
    }
    assert SignupRequest(**base, business_timezone="US/Pacific").business_timezone == "US/Pacific"
    with pytest.raises(ValidationError) as exc:
        SignupRequest(**base, business_timezone="Mars/Phobos")
    assert _error_fields(exc.value) == {"business_timezone"}


# --------------------------------------------------------------------------------- Settings


def test_absent_fields_are_unset_and_not_applied():
    payload = _patch(name="  New Name  ")
    assert payload.name == "New Name"
    assert payload.model_dump(exclude_unset=True, exclude={"version"}) == {"name": "New Name"}


def test_blank_optional_strings_become_null_and_text_is_trimmed():
    payload = _patch(business_type="  ", contact_phone="  555  ", address_text="")
    assert payload.business_type is None
    assert payload.contact_phone == "555"
    assert payload.address_text is None


def test_no_length_limits_are_invented_for_ungrounded_text_fields():
    long_text = "x" * 50_000
    payload = _patch(business_type=long_text, contact_phone=long_text, address_text=long_text)
    assert payload.business_type == long_text
    assert payload.contact_phone == long_text
    assert payload.address_text == long_text


def test_contact_email_shape_is_checked_and_case_is_preserved():
    assert _patch(contact_email="  Shop@Example.COM ").contact_email == "Shop@Example.COM"
    assert _patch(contact_email="   ").contact_email is None
    with pytest.raises(ValidationError) as exc:
        _patch(contact_email="not-an-email")
    assert _error_fields(exc.value) == {"contact_email"}


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "timezone",
        "default_labor_rate",
        "fulfillment_warning_minutes",
        "shopping_horizon_days",
    ],
)
def test_required_fields_cannot_be_cleared_with_null(field):
    with pytest.raises(ValidationError) as exc:
        BusinessSettingsUpdateRequest(version=1, **{field: None})
    assert _error_fields(exc.value) == {field}


def test_nullable_fields_can_be_cleared_with_explicit_null():
    payload = _patch(
        business_type=None,
        contact_phone=None,
        contact_email=None,
        address_text=None,
        target_contribution_margin=None,
    )
    assert payload.model_dump(exclude_unset=True, exclude={"version"}) == {
        "business_type": None,
        "contact_phone": None,
        "contact_email": None,
        "address_text": None,
        "target_contribution_margin": None,
    }


def test_blank_business_name_is_rejected():
    with pytest.raises(ValidationError):
        _patch(name="   ")


@pytest.mark.parametrize("value", ["0", "0.00", "12.34", "999999999999.99"])
def test_valid_labor_rates(value):
    assert _patch(default_labor_rate=value).default_labor_rate == D(value)


@pytest.mark.parametrize("value", ["-0.01", "1.234", "10000000000000.00", "NaN", "Infinity"])
def test_invalid_labor_rates(value):
    with pytest.raises(ValidationError) as exc:
        _patch(default_labor_rate=value)
    assert _error_fields(exc.value) == {"default_labor_rate"}


@pytest.mark.parametrize("value", ["0", "0.5", "0.625", "0.999999", "0.000001"])
def test_valid_margins(value):
    assert _patch(target_contribution_margin=value).target_contribution_margin == D(value)


@pytest.mark.parametrize("value", ["-0.000001", "1", "1.5", "0.0000001", "NaN"])
def test_invalid_margins(value):
    with pytest.raises(ValidationError) as exc:
        _patch(target_contribution_margin=value)
    assert _error_fields(exc.value) == {"target_contribution_margin"}


@pytest.mark.parametrize("value", [1, 60, 1440, 100_000, POSTGRES_INTEGER_MAX])
def test_valid_warning_minutes(value):
    assert _patch(fulfillment_warning_minutes=value).fulfillment_warning_minutes == value


@pytest.mark.parametrize("value", [0, -1, POSTGRES_INTEGER_MAX + 1, 1.5, "60", True])
def test_invalid_warning_minutes(value):
    with pytest.raises(ValidationError) as exc:
        _patch(fulfillment_warning_minutes=value)
    assert _error_fields(exc.value) == {"fulfillment_warning_minutes"}


def test_warning_minutes_has_no_invented_product_maximum():
    # 10,080 (one week) was explicitly rejected as a product limit.
    assert _patch(fulfillment_warning_minutes=10_081).fulfillment_warning_minutes == 10_081
    assert _patch(fulfillment_warning_minutes=10_000_000).fulfillment_warning_minutes == 10_000_000


@pytest.mark.parametrize("value", [3, 7])
def test_valid_horizon(value):
    assert _patch(shopping_horizon_days=value).shopping_horizon_days == value


@pytest.mark.parametrize("value", [0, 2, 4, 5, 6, 8, 14])
def test_invalid_horizon(value):
    with pytest.raises(ValidationError) as exc:
        _patch(shopping_horizon_days=value)
    assert _error_fields(exc.value) == {"shopping_horizon_days"}


def test_invalid_timezone_in_settings_is_a_field_error():
    with pytest.raises(ValidationError) as exc:
        _patch(timezone="Mars/Phobos")
    assert _error_fields(exc.value) == {"timezone"}


def test_unknown_and_foreign_keys_are_ignored_not_applied():
    payload = BusinessSettingsUpdateRequest(
        version=1,
        name="X",
        business_id="00000000-0000-0000-0000-000000000000",
        owner_user_id="00000000-0000-0000-0000-000000000000",
        id="00000000-0000-0000-0000-000000000000",
        default_fulfillment_method="PICKUP",
    )
    assert payload.model_dump(exclude_unset=True, exclude={"version"}) == {"name": "X"}


def test_response_quantizes_money_and_ratio_without_floats():
    response = BusinessSettingsResponse(
        id="x",
        name="B",
        business_type=None,
        contact_phone=None,
        contact_email=None,
        address_text=None,
        timezone="UTC",
        default_labor_rate=D("5"),
        target_contribution_margin=D("0.625"),
        fulfillment_warning_minutes=60,
        shopping_horizon_days=7,
        version=1,
    )
    dumped = response.model_dump(mode="json")
    assert dumped["default_labor_rate"] == "5.00"
    assert dumped["target_contribution_margin"] == "0.625000"


def test_owner_name_is_trimmed_and_required():
    assert OwnerProfileUpdateRequest(name="  Pat  ").name == "Pat"
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            OwnerProfileUpdateRequest(name=bad)
    with pytest.raises(ValidationError):
        OwnerProfileUpdateRequest(name="x" * 201)


def test_owner_request_ignores_email_and_password():
    payload = OwnerProfileUpdateRequest(
        name="Pat", email="evil@example.com", password="x", password_hash="y"
    )
    assert payload.model_dump() == {"name": "Pat"}
