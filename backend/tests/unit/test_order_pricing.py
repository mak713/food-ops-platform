"""Unit tests for the deterministic Order-pricing domain module (Phase 6 Final Plan §J;
Final Pre-Implementation Amendment)."""

from __future__ import annotations

from decimal import Decimal

from app.domain.order_pricing import (
    MONEY_MAX_MAGNITUDE,
    QUANTITY_MAX_MAGNITUDE,
    ConfirmationIssue,
    PaymentStatus,
    calculate_custom_item_line,
    calculate_custom_quantity_line,
    calculate_line_subtotal,
    calculate_order_totals,
    calculate_packaging_total,
    calculate_standard_option_line,
    check_structural_confirmation_readiness,
    derive_payment_status,
    is_representable_in_numeric_14_2,
    is_representable_in_numeric_18_6,
    quantize_money,
    quantize_quantity,
    rescale_standard_option_line,
)

# --- Worked examples (Spec §8.16) ------------------------------------------------------


def test_standard_option_worked_example_2x_6pack_at_12():
    snapshot = calculate_standard_option_line(
        display_name="Sourdough Loaf — 6-pack",
        package_quantity=Decimal("2"),
        underlying_units_per_package=Decimal("6"),
        charged_unit_price=Decimal("12.00"),
        packaging_cost_per_package=Decimal("0"),
    )
    assert snapshot.package_quantity == Decimal("2")
    assert snapshot.underlying_quantity == Decimal("12")
    assert snapshot.charged_unit_price_snapshot == Decimal("12.00")
    assert snapshot.line_subtotal == Decimal("24.00")


def test_custom_quantity_worked_example_30_cookies_for_55():
    snapshot = calculate_custom_quantity_line(
        display_name="Chocolate Chip Cookie",
        underlying_quantity=Decimal("30"),
        agreed_line_price=Decimal("55.00"),
        packaging_cost_per_package=Decimal("0"),
    )
    assert snapshot.package_quantity == Decimal("1")
    assert snapshot.underlying_quantity == Decimal("30")
    assert snapshot.charged_unit_price_snapshot == Decimal("55.00")
    assert snapshot.line_subtotal == Decimal("55.00")


def test_custom_item_line_no_packaging_and_quantity_equals_package_quantity():
    snapshot = calculate_custom_item_line(
        display_name="Custom cake topper", quantity=Decimal("3"), unit_price=Decimal("10.00")
    )
    assert snapshot.package_quantity == Decimal("3")
    assert snapshot.underlying_quantity == Decimal("3")
    assert snapshot.line_subtotal == Decimal("30.00")
    assert snapshot.packaging_cost_per_package_snapshot == Decimal("0")
    assert snapshot.packaging_cost_total_snapshot == Decimal("0")


# --- rescale_standard_option_line: quantity-only edit never re-derives from catalog ---


def test_rescale_reapplies_stored_ratio_and_rates_not_live_catalog():
    # Originally captured as 2 x 6-pack @ $12, packaging $0.50/package.
    rescaled = rescale_standard_option_line(
        display_name="Sourdough Loaf — 6-pack",
        new_package_quantity=Decimal("3"),
        stored_package_quantity=Decimal("2"),
        stored_underlying_quantity=Decimal("12"),
        stored_charged_unit_price_snapshot=Decimal("12.00"),
        stored_packaging_cost_per_package_snapshot=Decimal("0.50"),
    )
    assert rescaled.package_quantity == Decimal("3")
    # ratio 12/2 = 6 units/package, reapplied: 3 * 6 = 18 — never re-reading a live
    # Selling Option multiplier that may have since changed.
    assert rescaled.underlying_quantity == Decimal("18")
    assert rescaled.charged_unit_price_snapshot == Decimal("12.00")
    assert rescaled.line_subtotal == Decimal("36.00")
    assert rescaled.packaging_cost_total_snapshot == Decimal("1.50")


# --- calculate_line_subtotal / calculate_packaging_total ------------------------------


def test_calculate_line_subtotal_and_packaging_total():
    assert calculate_line_subtotal(Decimal("4"), Decimal("2.50")) == Decimal("10.00")
    assert calculate_packaging_total(Decimal("4"), Decimal("0.25")) == Decimal("1.00")


# --- calculate_order_totals ------------------------------------------------------------


def test_order_totals_subtotal_and_final_total():
    totals = calculate_order_totals(
        [Decimal("24.00"), Decimal("55.00")],
        order_adjustment=Decimal("-5.00"),
        manual_tax=Decimal("3.00"),
    )
    assert totals.subtotal == Decimal("79.00")
    assert totals.final_total == Decimal("77.00")


def test_order_totals_with_no_lines():
    totals = calculate_order_totals([], order_adjustment=Decimal("0"), manual_tax=Decimal("0"))
    assert totals.subtotal == Decimal("0")
    assert totals.final_total == Decimal("0")


# --- derive_payment_status: AC-PAY-002 thresholds, zero-dollar-Order case -------------


def test_payment_status_unpaid_when_zero():
    assert derive_payment_status(Decimal("0"), Decimal("100.00")) is PaymentStatus.UNPAID


def test_payment_status_partially_paid():
    status = derive_payment_status(Decimal("40.00"), Decimal("100.00"))
    assert status is PaymentStatus.PARTIALLY_PAID


def test_payment_status_paid_exact():
    assert derive_payment_status(Decimal("100.00"), Decimal("100.00")) is PaymentStatus.PAID


def test_payment_status_paid_overpaid():
    assert derive_payment_status(Decimal("150.00"), Decimal("100.00")) is PaymentStatus.PAID


def test_zero_dollar_order_with_zero_payments_is_unpaid_not_paid():
    """Final Pre-Implementation Amendment §10: $0 Order + $0 recorded Payments -> UNPAID,
    never PAID merely because 0 >= 0."""
    assert derive_payment_status(Decimal("0"), Decimal("0")) is PaymentStatus.UNPAID


def test_positive_payment_against_zero_dollar_order_is_paid():
    """Any positive Payment against a $0.00 Order is inherently an overpayment (checked at
    the service layer, not here) — but once recorded, the derived status is PAID."""
    assert derive_payment_status(Decimal("10.00"), Decimal("0")) is PaymentStatus.PAID


# --- Decimal quantization / representability -------------------------------------------


def test_quantize_money_rounds_half_up_to_cent():
    assert quantize_money(Decimal("1.005")) == Decimal("1.01")
    assert quantize_money(Decimal("1.004")) == Decimal("1.00")


def test_quantize_quantity_rounds_half_up_to_six_places():
    assert quantize_quantity(Decimal("1.0000005")) == Decimal("1.000001")
    assert quantize_quantity(Decimal("1.0000004")) == Decimal("1.000000")


def test_money_overflow_rejected():
    assert is_representable_in_numeric_14_2(MONEY_MAX_MAGNITUDE) is True
    assert is_representable_in_numeric_14_2(MONEY_MAX_MAGNITUDE + Decimal("0.01")) is False


def test_quantity_overflow_rejected():
    assert is_representable_in_numeric_18_6(QUANTITY_MAX_MAGNITUDE) is True
    assert is_representable_in_numeric_18_6(QUANTITY_MAX_MAGNITUDE + Decimal("0.000001")) is False


def test_positive_quantity_collapsing_to_zero_is_detectable():
    tiny = Decimal("0.0000001")  # rounds to 0.000000 at 6 decimal places
    quantized = quantize_quantity(tiny)
    assert quantized == Decimal("0")
    # The service layer is expected to reject a quantized quantity that is <= 0 even
    # though the original input was positive — this test documents the collapse itself.


# --- check_structural_confirmation_readiness (ORD-004 only; never mutates anything) ---


def test_readiness_all_clear():
    issues = check_structural_confirmation_readiness(
        status="DRAFT", line_count=1, fulfillment_date_present=True
    )
    assert issues == []


def test_readiness_missing_line():
    issues = check_structural_confirmation_readiness(
        status="DRAFT", line_count=0, fulfillment_date_present=True
    )
    codes = [i.code for i in issues]
    assert "ORDER_NO_LINES" in codes


def test_readiness_missing_fulfillment_date():
    issues = check_structural_confirmation_readiness(
        status="DRAFT", line_count=1, fulfillment_date_present=False
    )
    codes = [i.code for i in issues]
    assert "ORDER_MISSING_FULFILLMENT_DATE" in codes


def test_readiness_wrong_status():
    issues = check_structural_confirmation_readiness(
        status="CONFIRMED", line_count=1, fulfillment_date_present=True
    )
    codes = [i.code for i in issues]
    assert "ORDER_STATUS_NOT_DRAFT" in codes


def test_readiness_issue_is_typed_not_a_bare_string():
    issues = check_structural_confirmation_readiness(
        status="DRAFT", line_count=0, fulfillment_date_present=False
    )
    for issue in issues:
        assert isinstance(issue, ConfirmationIssue)
        assert issue.severity == "ERROR"
        assert issue.code
        assert issue.message
