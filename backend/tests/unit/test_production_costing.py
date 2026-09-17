"""Unit tests for planned-cost, workload, and suggested-start calculators (Phase 7
Plan v2 §4/§13, corrected by Final Pre-Implementation Amendment §1/§2 and Final
Architecture Lock §G)."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from app.domain.production_costing import (
    LocalTimeValidity,
    SuggestedStartStatus,
    WorkloadResult,
    calculate_planned_labor_cost,
    calculate_suggested_start,
    calculate_workload,
    resolve_planned_ingredient_unit_cost,
    validate_business_local_datetime,
)

# --- Planned ingredient cost basis: WAC-first (Amendment §1) --------------------------


def test_positive_physical_quantity_uses_weighted_average_even_when_zero():
    result = resolve_planned_ingredient_unit_cost(
        physical_quantity=Decimal("10"),
        weighted_average_unit_cost=Decimal("0"),
        replacement_unit_cost=Decimal("5.00"),
        latest_purchase_unit_cost=Decimal("4.00"),
    )
    assert result == Decimal("0")


def test_positive_physical_quantity_uses_weighted_average_ignoring_replacement():
    result = resolve_planned_ingredient_unit_cost(
        physical_quantity=Decimal("10"),
        weighted_average_unit_cost=Decimal("2.50"),
        replacement_unit_cost=Decimal("9.99"),
        latest_purchase_unit_cost=Decimal("9.99"),
    )
    assert result == Decimal("2.50")


def test_non_positive_physical_quantity_falls_back_to_replacement_override():
    result = resolve_planned_ingredient_unit_cost(
        physical_quantity=Decimal("0"),
        weighted_average_unit_cost=Decimal("2.50"),
        replacement_unit_cost=Decimal("9.99"),
        latest_purchase_unit_cost=Decimal("4.00"),
    )
    assert result == Decimal("9.99")


def test_negative_physical_quantity_falls_back_to_latest_purchase_when_no_override():
    result = resolve_planned_ingredient_unit_cost(
        physical_quantity=Decimal("-5"),
        weighted_average_unit_cost=Decimal("2.50"),
        replacement_unit_cost=None,
        latest_purchase_unit_cost=Decimal("4.00"),
    )
    assert result == Decimal("4.00")


def test_non_positive_physical_quantity_with_no_fallback_is_unknown_not_zero():
    result = resolve_planned_ingredient_unit_cost(
        physical_quantity=Decimal("0"),
        weighted_average_unit_cost=Decimal("2.50"),
        replacement_unit_cost=None,
        latest_purchase_unit_cost=None,
    )
    assert result is None


# --- Labor cost / workload -----------------------------------------------------------


def test_labor_cost_formula():
    result = calculate_planned_labor_cost(
        batches=3, active_minutes_per_batch=20, labor_rate=Decimal("15.00")
    )
    assert result == Decimal(3 * 20 * 15) / Decimal(60)


def test_workload_active_and_elapsed():
    result = calculate_workload(
        batches=4, active_minutes_per_batch=10, elapsed_minutes_per_batch=25
    )
    assert result == WorkloadResult(estimated_active_minutes=40, estimated_elapsed_minutes=100)


def test_workload_elapsed_none_when_unknown():
    result = calculate_workload(
        batches=4, active_minutes_per_batch=10, elapsed_minutes_per_batch=None
    )
    assert result.estimated_elapsed_minutes is None


# --- Suggested start: normal + missing-input cases ------------------------------------


def test_suggested_start_normal_case():
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 6, 15),
        fulfillment_time=time(14, 0),
        elapsed_minutes=90,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.OK
    # 2026-06-15 is during EDT (UTC-4); 14:00 local - 90min = 12:30 local = 16:30 UTC.
    expected_tzinfo = result.suggested_start_at_utc.tzinfo
    assert result.suggested_start_at_utc == datetime(2026, 6, 15, 16, 30, tzinfo=expected_tzinfo)


def test_suggested_start_missing_time_is_not_an_error():
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 6, 15),
        fulfillment_time=None,
        elapsed_minutes=90,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.MISSING_INPUT
    assert result.suggested_start_at_utc is None


def test_suggested_start_missing_elapsed_duration_is_not_an_error():
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 6, 15),
        fulfillment_time=time(14, 0),
        elapsed_minutes=None,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.MISSING_INPUT
    assert result.suggested_start_at_utc is None


# --- Suggested start: DST edge cases (America/New_York, verified 2026 transitions) ----


def test_suggested_start_nonexistent_local_time_rejected():
    """2026-03-08 is the US spring-forward date for America/New_York — 02:00-02:59
    local time does not exist that day."""
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 3, 8),
        fulfillment_time=time(2, 30),
        elapsed_minutes=30,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.NONEXISTENT_LOCAL_TIME
    assert result.suggested_start_at_utc is None


def test_suggested_start_ambiguous_local_time_rejected():
    """2026-11-01 is the US fall-back date for America/New_York — 01:00-01:59 local
    time occurs twice that day."""
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 11, 1),
        fulfillment_time=time(1, 30),
        elapsed_minutes=30,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.AMBIGUOUS_LOCAL_TIME
    assert result.suggested_start_at_utc is None


def test_suggested_start_arithmetic_overflow_reported_not_raised():
    result = calculate_suggested_start(
        fulfillment_date=date(2026, 6, 15),
        fulfillment_time=time(14, 0),
        elapsed_minutes=10**11,
        business_timezone="America/New_York",
    )
    assert result.status == SuggestedStartStatus.ARITHMETIC_OVERFLOW
    assert result.suggested_start_at_utc is None


# --- validate_business_local_datetime: extracted pure wall-clock DST check -----------
# (Phase 7 Final Lifecycle Invariant Correction Plan, Finding C)


def test_validate_business_local_datetime_valid():
    result = validate_business_local_datetime(
        local_date=date(2026, 6, 15),
        local_time=time(14, 0),
        business_timezone="America/New_York",
    )
    assert result is LocalTimeValidity.VALID


def test_validate_business_local_datetime_nonexistent():
    """2026-03-08 is the US spring-forward date for America/New_York — 02:00-02:59
    local time does not exist that day."""
    result = validate_business_local_datetime(
        local_date=date(2026, 3, 8),
        local_time=time(2, 30),
        business_timezone="America/New_York",
    )
    assert result is LocalTimeValidity.NONEXISTENT


def test_validate_business_local_datetime_ambiguous():
    """2026-11-01 is the US fall-back date for America/New_York — 01:00-01:59 local
    time occurs twice that day."""
    result = validate_business_local_datetime(
        local_date=date(2026, 11, 1),
        local_time=time(1, 30),
        business_timezone="America/New_York",
    )
    assert result is LocalTimeValidity.AMBIGUOUS
