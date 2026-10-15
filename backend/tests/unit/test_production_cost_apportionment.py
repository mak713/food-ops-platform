"""Unit tests for the exact integer production-cost apportionment (Phase 8 Final
Architecture Amendment §2 / Final Architecture Lock Addendum §B).

Every "exact" assertion here checks a literal equality on a persistence-precision
Decimal, not an approximate/tolerance comparison — that is the entire point of the
Hamilton/largest-remainder method this module implements."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.domain.production_cost_apportionment import (
    CostRecipient,
    apportion_production_cost,
)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def test_all_direct_allocation_sums_exactly_to_run_cost():
    a, b = _uid(), _uid()
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("100.000000"),
        actual_output_quantity=Decimal("10.000000"),
        direct_recipients=[
            CostRecipient(order_line_id=a, quantity=Decimal("6.000000")),
            CostRecipient(order_line_id=b, quantity=Decimal("4.000000")),
        ],
        surplus_excess_quantity=Decimal("0"),
    )
    assert result.order_line_amounts[a] == Decimal("60.00")
    assert result.order_line_amounts[b] == Decimal("40.00")
    assert sum(result.order_line_amounts.values()) == Decimal("100.00")
    assert result.surplus_source_total_cost_basis is None


def test_mixed_direct_and_surplus_sums_exactly_to_run_cost():
    a = _uid()
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("100.000000"),
        actual_output_quantity=Decimal("10.000000"),
        direct_recipients=[CostRecipient(order_line_id=a, quantity=Decimal("7.000000"))],
        surplus_excess_quantity=Decimal("3.000000"),
    )
    total = sum(result.order_line_amounts.values()) + result.surplus_source_total_cost_basis
    assert total == Decimal("100.00")
    assert result.order_line_amounts[a] == Decimal("70.00")
    assert result.surplus_source_total_cost_basis == Decimal("30.00")


def test_all_surplus_receives_entire_run_cost_exactly():
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("42.500000"),
        actual_output_quantity=Decimal("5.000000"),
        direct_recipients=[],
        surplus_excess_quantity=Decimal("5.000000"),
    )
    assert result.order_line_amounts == {}
    assert result.surplus_source_total_cost_basis == Decimal("42.50")


def test_repeating_ratio_100_over_3_preserves_exact_total():
    """The exact case the review's own example targets: 100.00 / 3 never terminates
    at any finite precision, so this can only pass if the method is genuinely exact
    integer apportionment, not Decimal division."""
    a, b, c = _uid(), _uid(), _uid()
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("100.000000"),
        actual_output_quantity=Decimal("3.000000"),
        direct_recipients=[
            CostRecipient(order_line_id=a, quantity=Decimal("1.000000")),
            CostRecipient(order_line_id=b, quantity=Decimal("1.000000")),
            CostRecipient(order_line_id=c, quantity=Decimal("1.000000")),
        ],
        surplus_excess_quantity=Decimal("0"),
    )
    assert sum(result.order_line_amounts.values()) == Decimal("100.00")
    # Largest-remainder ties broken by established priority order (a, b, c as passed).
    amounts = [result.order_line_amounts[x] for x in (a, b, c)]
    assert sorted(amounts, reverse=True) == amounts  # earlier recipients never lose a tie
    assert all(amt in (Decimal("33.33"), Decimal("33.34")) for amt in amounts)


def test_repeating_ratio_with_surplus_still_preserves_exact_total():
    a = _uid()
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("10.000000"),
        actual_output_quantity=Decimal("3.000000"),
        direct_recipients=[CostRecipient(order_line_id=a, quantity=Decimal("1.000000"))],
        surplus_excess_quantity=Decimal("2.000000"),
    )
    total = result.order_line_amounts[a] + result.surplus_source_total_cost_basis
    assert total == Decimal("10.00")


def test_many_fractional_cent_recipients_preserve_exact_total():
    recipients = [
        CostRecipient(order_line_id=_uid(), quantity=Decimal("1.000000")) for _ in range(7)
    ]
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("10.000000"),
        actual_output_quantity=Decimal("7.000000"),
        direct_recipients=recipients,
        surplus_excess_quantity=Decimal("0"),
    )
    assert sum(result.order_line_amounts.values()) == Decimal("10.00")
    assert len(result.order_line_amounts) == 7


def test_six_decimal_quantity_precision_preserves_exact_total():
    a, b = _uid(), _uid()
    result = apportion_production_cost(
        actual_total_production_cost=Decimal("17.123456"),
        actual_output_quantity=Decimal("1.500000"),
        direct_recipients=[
            CostRecipient(order_line_id=a, quantity=Decimal("0.333333")),
            CostRecipient(order_line_id=b, quantity=Decimal("1.166667")),
        ],
        surplus_excess_quantity=Decimal("0"),
    )
    assert sum(result.order_line_amounts.values()) == Decimal("17.12")


def test_mismatched_recipient_quantities_raise():
    a = _uid()
    try:
        apportion_production_cost(
            actual_total_production_cost=Decimal("10.000000"),
            actual_output_quantity=Decimal("5.000000"),
            direct_recipients=[CostRecipient(order_line_id=a, quantity=Decimal("2.000000"))],
            surplus_excess_quantity=Decimal("2.000000"),  # 2 + 2 != 5
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_property_random_inputs_always_preserve_exact_total():
    """Property-style: exactness must hold for arbitrary quantity/cost combinations,
    not just the hand-picked cases above."""
    import random

    rng = random.Random(20260917)
    for _ in range(500):
        recipient_count = rng.randint(0, 5)
        has_surplus = rng.random() < 0.5
        recipient_micros = [rng.randint(1, 5_000_000) for _ in range(recipient_count)]
        surplus_micros = rng.randint(1, 5_000_000) if has_surplus else 0
        total_micros = sum(recipient_micros) + surplus_micros
        if total_micros == 0:
            continue
        cost_micros = rng.randint(1, 100_000_000)

        recipients = [
            CostRecipient(order_line_id=_uid(), quantity=Decimal(m) / Decimal(1_000_000))
            for m in recipient_micros
        ]
        result = apportion_production_cost(
            actual_total_production_cost=Decimal(cost_micros) / Decimal(1_000_000),
            actual_output_quantity=Decimal(total_micros) / Decimal(1_000_000),
            direct_recipients=recipients,
            surplus_excess_quantity=Decimal(surplus_micros) / Decimal(1_000_000),
        )
        total = sum(result.order_line_amounts.values())
        if result.surplus_source_total_cost_basis is not None:
            total += result.surplus_source_total_cost_basis
        expected = (Decimal(cost_micros) / Decimal(1_000_000)).quantize(Decimal("0.01"))
        assert total == expected, (recipient_micros, surplus_micros, cost_micros)


def test_zero_output_quantity_raises():
    try:
        apportion_production_cost(
            actual_total_production_cost=Decimal("10.000000"),
            actual_output_quantity=Decimal("0"),
            direct_recipients=[],
            surplus_excess_quantity=Decimal("0"),
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
