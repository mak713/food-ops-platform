"""Unit tests for the pure Order Completion planner (Order Completion slice): purchased WAC
fulfillment, Surplus consumption, Packaging/Custom Direct snapshots, known-zero vs absent
costs, cents/6dp quantization, aggregate sufficiency, and Decimal-context isolation."""

from __future__ import annotations

import decimal
import uuid
from decimal import Decimal

from app.domain import order_completion as oc

P1, P2 = uuid.uuid4(), uuid.uuid4()
LOT_A, LOT_B = uuid.uuid4(), uuid.uuid4()


def _id() -> uuid.UUID:
    return uuid.uuid4()


def _plan(
    *,
    purchased_lines=(),
    purchased_inventory=None,
    allocations=(),
    lots=None,
    snapshots=(),
):
    return oc.build_completion_plan(
        purchased_lines=list(purchased_lines),
        purchased_inventory=purchased_inventory or {},
        surplus_allocations=list(allocations),
        surplus_lots=lots or {},
        line_snapshots=list(snapshots),
    )


def _inv(product_id, physical, wac):
    return oc.PurchasedInventoryFact(product_id, Decimal(physical), Decimal(wac))


# ------------------------------------------------------------------ Purchased


def test_purchased_cost_uses_wac_snapshot_quantized_once_per_stage():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("3"))
    plan, failures = _plan(
        purchased_lines=[line], purchased_inventory={P1: _inv(P1, "10", "0.333333")}
    )
    assert failures == []
    p = plan.purchased[0]
    assert p.unit_cost == Decimal("0.333333")  # the WAC snapshot, unchanged
    assert p.total_cost == Decimal("0.999999")  # qty x unit, 6dp once
    assert p.ledger_amount == Decimal("1.00")  # persisted total -> cents once (half-up)
    assert plan.purchased_deduction_by_product == {P1: Decimal("3")}


def test_purchased_half_up_cents_rounding():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("1"))
    plan, _ = _plan(purchased_lines=[line], purchased_inventory={P1: _inv(P1, "5", "2.675")})
    assert plan.purchased[0].ledger_amount == Decimal("2.68")


def test_zero_wac_is_a_known_zero_row_not_an_absent_one():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("4"))
    plan, failures = _plan(purchased_lines=[line], purchased_inventory={P1: _inv(P1, "4", "0")})
    assert failures == []
    assert len(plan.purchased) == 1
    assert plan.purchased[0].ledger_amount == Decimal("0.00")


def test_multiple_lines_for_the_same_product_aggregate_and_each_keeps_provenance():
    l1, l2 = (
        oc.PurchasedLineFact(_id(), P1, Decimal("3")),
        oc.PurchasedLineFact(_id(), P1, Decimal("4")),
    )
    plan, failures = _plan(
        purchased_lines=[l1, l2], purchased_inventory={P1: _inv(P1, "7", "1.50")}
    )
    assert failures == []
    assert plan.purchased_deduction_by_product == {P1: Decimal("7")}
    assert {p.line_id for p in plan.purchased} == {l1.line_id, l2.line_id}
    assert sorted(p.ledger_amount for p in plan.purchased) == [Decimal("4.50"), Decimal("6.00")]
    assert len({p.transaction_id for p in plan.purchased}) == 2


def test_aggregate_sufficiency_not_line_by_line():
    # Each line (4) fits within 7 on hand, but together (8) they do not.
    l1, l2 = (
        oc.PurchasedLineFact(_id(), P1, Decimal("4")),
        oc.PurchasedLineFact(_id(), P1, Decimal("4")),
    )
    plan, failures = _plan(purchased_lines=[l1, l2], purchased_inventory={P1: _inv(P1, "7", "1")})
    assert plan is None
    assert [f.code for f in failures] == ["PURCHASED_STOCK_INSUFFICIENT"]
    assert failures[0].details["required_quantity"] == "8"


def test_exactly_sufficient_stock_is_allowed_and_leaves_zero():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("7"))
    plan, failures = _plan(purchased_lines=[line], purchased_inventory={P1: _inv(P1, "7", "1")})
    assert failures == [] and plan is not None


def test_missing_inventory_row_means_zero_stock():
    line = oc.PurchasedLineFact(_id(), P2, Decimal("1"))
    plan, failures = _plan(purchased_lines=[line])
    assert plan is None and failures[0].code == "PURCHASED_STOCK_INSUFFICIENT"


# ------------------------------------------------------------------- Surplus


def _alloc(lot, line, qty):
    return oc.SurplusAllocationFact(_id(), lot, line, Decimal(qty))


def test_surplus_amount_is_quantity_times_lot_basis_in_cents_half_up():
    line = _id()
    lots = {LOT_A: oc.SurplusLotFact(LOT_A, Decimal("10"), Decimal("0.835000"))}
    plan, failures = _plan(allocations=[_alloc(LOT_A, line, "3")], lots=lots)
    assert failures == []
    assert plan.surplus[0].ledger_amount == Decimal("2.51")  # 2.505 -> 2.51
    assert plan.surplus_consumption_by_lot == {LOT_A: Decimal("3")}


def test_zero_surplus_basis_writes_an_explicit_zero_row():
    line = _id()
    lots = {LOT_A: oc.SurplusLotFact(LOT_A, Decimal("5"), Decimal("0"))}
    plan, _ = _plan(allocations=[_alloc(LOT_A, line, "5")], lots=lots)
    assert len(plan.surplus) == 1 and plan.surplus[0].ledger_amount == Decimal("0.00")


def test_multiple_lots_on_one_line_stay_distinct_sources_with_their_own_basis():
    line = _id()
    lots = {
        LOT_A: oc.SurplusLotFact(LOT_A, Decimal("4"), Decimal("0.50")),
        LOT_B: oc.SurplusLotFact(LOT_B, Decimal("6"), Decimal("1.25")),
    }
    plan, failures = _plan(
        allocations=[_alloc(LOT_A, line, "4"), _alloc(LOT_B, line, "2")], lots=lots
    )
    assert failures == []
    by_lot = {s.lot_id: s for s in plan.surplus}
    assert by_lot[LOT_A].ledger_amount == Decimal("2.00")
    assert by_lot[LOT_B].ledger_amount == Decimal("2.50")
    assert {s.line_id for s in plan.surplus} == {line}


def test_surplus_lot_shortfall_is_rejected_never_driven_negative():
    line = _id()
    lots = {LOT_A: oc.SurplusLotFact(LOT_A, Decimal("2"), Decimal("1"))}
    plan, failures = _plan(allocations=[_alloc(LOT_A, line, "3")], lots=lots)
    assert plan is None and failures[0].code == "SURPLUS_STOCK_INSUFFICIENT"


def test_surplus_sufficiency_aggregates_per_lot_across_lines():
    lots = {LOT_A: oc.SurplusLotFact(LOT_A, Decimal("5"), Decimal("1"))}
    plan, failures = _plan(
        allocations=[_alloc(LOT_A, _id(), "3"), _alloc(LOT_A, _id(), "3")], lots=lots
    )
    assert plan is None and failures[0].details["required_quantity"] == "6"


# ------------------------------------------------------- Packaging / Custom Direct


def test_packaging_row_for_every_line_including_explicit_zero_and_exact_copy():
    a, b = _id(), _id()
    plan, _ = _plan(
        snapshots=[
            oc.LineSnapshotFact(a, Decimal("1.10"), None),
            oc.LineSnapshotFact(b, Decimal("0.00"), None),
        ]
    )
    assert {p.line_id: p.amount for p in plan.packaging} == {a: Decimal("1.10"), b: Decimal("0.00")}


def test_custom_direct_null_creates_no_row_zero_creates_a_zero_row():
    none_line, zero_line, positive_line = _id(), _id(), _id()
    plan, _ = _plan(
        snapshots=[
            oc.LineSnapshotFact(none_line, Decimal("0"), None),
            oc.LineSnapshotFact(zero_line, Decimal("0"), Decimal("0.00")),
            oc.LineSnapshotFact(positive_line, Decimal("0"), Decimal("7.25")),
        ]
    )
    amounts = {c.line_id: c.amount for c in plan.custom_direct}
    assert none_line not in amounts
    assert amounts == {zero_line: Decimal("0.00"), positive_line: Decimal("7.25")}


# ------------------------------------------------------------------- Whole plan


def test_expected_ledger_total_is_the_sum_of_all_planned_rows():
    l1, line_s = _id(), _id()
    lots = {LOT_A: oc.SurplusLotFact(LOT_A, Decimal("10"), Decimal("0.50"))}
    plan, _ = _plan(
        purchased_lines=[oc.PurchasedLineFact(l1, P1, Decimal("2"))],
        purchased_inventory={P1: _inv(P1, "5", "1.25")},
        allocations=[_alloc(LOT_A, line_s, "4")],
        lots=lots,
        snapshots=[
            oc.LineSnapshotFact(l1, Decimal("0.40"), None),
            oc.LineSnapshotFact(line_s, Decimal("0.60"), Decimal("3.00")),
        ],
    )
    # 2.50 purchased + 2.00 surplus + 0.40 + 0.60 packaging + 3.00 custom
    assert plan.expected_ledger_total() == Decimal("8.50")


def test_plan_is_deterministic_regardless_of_input_order():
    a, b = _id(), _id()
    inv = {P1: _inv(P1, "20", "1")}
    forward, _ = _plan(
        purchased_lines=[
            oc.PurchasedLineFact(a, P1, Decimal("1")),
            oc.PurchasedLineFact(b, P1, Decimal("2")),
        ],
        purchased_inventory=inv,
    )
    backward, _ = _plan(
        purchased_lines=[
            oc.PurchasedLineFact(b, P1, Decimal("2")),
            oc.PurchasedLineFact(a, P1, Decimal("1")),
        ],
        purchased_inventory=inv,
    )
    assert [p.line_id for p in forward.purchased] == [p.line_id for p in backward.purchased]


def test_unrepresentable_amount_is_reported_not_raised():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("999999999999"))
    plan, failures = _plan(
        purchased_lines=[line],
        purchased_inventory={P1: _inv(P1, "999999999999", "999999999999")},
    )
    assert plan is None
    assert failures[0].code == "COMPLETION_AMOUNT_NOT_REPRESENTABLE"


def test_unaffected_by_corrupted_ambient_decimal_context():
    line = oc.PurchasedLineFact(_id(), P1, Decimal("3"))
    inv = {P1: _inv(P1, "10", "0.333333")}
    baseline, _ = _plan(purchased_lines=[line], purchased_inventory=inv)
    original = decimal.getcontext().copy()
    try:
        decimal.getcontext().prec = 2
        decimal.getcontext().rounding = decimal.ROUND_DOWN
        corrupted, _ = _plan(purchased_lines=[line], purchased_inventory=inv)
    finally:
        decimal.setcontext(original)
    assert (corrupted.purchased[0].total_cost, corrupted.purchased[0].ledger_amount) == (
        baseline.purchased[0].total_cost,
        baseline.purchased[0].ledger_amount,
    )
