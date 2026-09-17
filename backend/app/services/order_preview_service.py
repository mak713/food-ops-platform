"""Draft/Confirmed Operational Preview (Spec §11.5; Phase 7 Plan v2 §12, corrected by
the Final Pre-Implementation Amendment §4/§16 and the Phase 7 Implementation
Remediation Plan, Findings 2/4/5 and their amendment).

Reuses the exact same Phase 6 line-resolution rules (`order_service._reconcile_line`/
`_validate_and_lock_order_references`) and the same `recalculate_product_closure`
core real confirmation/confirmed-edit uses — never a second, independently-invented
operational interpretation, and no operational math is ever duplicated in the
frontend.

Covers all three cases:
- a brand-new, unsaved Order (`order_id=None`);
- unsaved edits to a persisted `DRAFT` Order (`order_id` set, `order.status ==
  DRAFT`) — current authoritative confirmed world plus the hypothetical proposed
  payload, unchanged from the original design (a DRAFT Order never counts as
  CONFIRMED demand in the first place, so no exclusion is needed);
- a hypothetical edit to a persisted `CONFIRMED` Order (`order_id` set,
  `order.status == CONFIRMED`) — the Amendment §4 substitution case: confirmed
  world MINUS this Order's own persisted contribution PLUS the hypothetical
  replacement, via `recalculate_product_closure`'s `exclude_order_id` parameter,
  never double-counting.

For every affected Product, TWO recalculation passes run (both `persist=False`,
zero-write): a BASELINE pass with no hypothetical demand, and a PROJECTED pass with
the hypothetical proposed lines folded in. The response exposes both, plus the
server-computed INCREMENTAL delta, so the frontend is never handed the whole
Product closure as though the Order being previewed caused it (Remediation Plan
amendment 2) — no operational delta reconstruction happens in React.

Historical RecipeRevision pins are preserved exactly as a real edit would: a
hypothetical line carries its REAL, stable `order_line_id` when it stands in for a
retained existing line (never a synthetic one for that case), so
`recalculate_product_closure`'s own pin-reuse logic (`_resolve_pin`) applies
identically whether the demand is real or hypothetical.

Zero writes: every lock this reuses is released by an unconditional rollback at the
end of `preview_order`, success or failure alike — never a commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.db.enums import OrderLineType, OrderStatus
from app.db.models.business import Business
from app.db.models.order import Order, OrderLine
from app.domain import demand_aggregation, order_pricing
from app.schemas.order import OrderCreateRequest
from app.services import order_service
from app.services.operational_recalculation_service import (
    HypotheticalLine,
    IngredientShortageResult,
    ProductionRequirementSummary,
    RecalcResult,
    compute_combined_ingredient_shortages,
    recalculate_product_closure,
    validate_fulfillment_local_time,
)
from app.services.order_lifecycle_service import (
    _collect_warning_fingerprints,
    _reject_if_confirmed_edit_touches_protected_demand,
    _structural_readiness_error,
    _warning_issues,
    get_order_production_locked_line_ids,
)


@dataclass(frozen=True)
class ProductionRequirementPreview:
    product_id: uuid.UUID
    recipe_revision_id: uuid.UUID | None
    demand_date: date
    is_protected: bool
    missing_recipe: bool
    baseline: ProductionRequirementSummary
    projected: ProductionRequirementSummary


@dataclass(frozen=True)
class IngredientAvailabilityPreview:
    ingredient_id: uuid.UUID
    ingredient_name: str
    canonical_unit: str
    physical_quantity: Decimal
    baseline_shortage_quantity: Decimal
    projected_shortage_quantity: Decimal


@dataclass(frozen=True)
class PurchasedShortagePreview:
    product_id: uuid.UUID
    baseline_shortage_quantity: Decimal
    projected_shortage_quantity: Decimal


@dataclass(frozen=True)
class CustomItemWorkloadPreview:
    """Baseline (confirmed-world) vs. projected (confirmed-world + hypothetical)
    manual Custom Item workload for one demand date (Phase 7 Final Remediation
    Correction Plan, Finding 6) — the same baseline/projected/incremental
    convention `ProductionRequirementPreview` already establishes, applied here to
    guidance that never touches a Product/Recipe/ProductionRequirement/Ingredient
    row (ORD-009/010)."""

    demand_date: date
    baseline_total_active_minutes: int
    projected_total_active_minutes: int
    baseline_contributing_order_line_ids: tuple[uuid.UUID, ...]
    projected_contributing_order_line_ids: tuple[uuid.UUID, ...]


_ZERO = Decimal("0")


def _zeroed_summary(
    template: ProductionRequirementSummary, *, product_id: uuid.UUID
) -> ProductionRequirementSummary:
    """Synthesizes a zero-demand summary for a group that exists on one side
    (baseline or projected) but not the other — e.g. the hypothetical edit removes
    a group's only demand entirely. Identity fields (product/revision/date) are
    reused from `template`; every quantity is zero/None, never fabricated."""
    return ProductionRequirementSummary(
        requirement_id=template.requirement_id,
        product_id=product_id,
        recipe_revision_id=template.recipe_revision_id,
        demand_date=template.demand_date,
        is_protected=template.is_protected,
        missing_recipe=template.missing_recipe,
        confirmed_demand_quantity=_ZERO,
        surplus_allocated_quantity=_ZERO,
        production_demand_quantity=_ZERO,
        recommended_batches=None,
        expected_output_quantity=None,
        expected_excess_quantity=None,
        estimated_active_minutes=None,
        estimated_elapsed_minutes=None,
        estimated_ingredient_cost=None,
        estimated_labor_cost=None,
        estimated_direct_production_cost=None,
        suggested_start_at=None,
    )


def _combined_shortages_for_results(
    db: Session, business: Business, results: list[RecalcResult]
) -> dict[uuid.UUID, IngredientShortageResult]:
    """Phase 7 Final Remediation Correction Plan, Finding 5 — the single
    authoritative per-Ingredient read across every affected Product's OWN result
    from the SAME `persist=False` pass (baseline or projected, called once each):
    `exclude_requirement_ids` is the union of every affected Product's own
    `rebuildable_requirement_ids` (each result's own prior candidate state, never
    "external" to this same pass), and `extra_candidate` is the union-summed
    `candidate_ingredient_totals` across every affected Product — exactly what a
    real Confirm/confirmed-edit's own `order_lifecycle_service.
    _combined_ingredient_shortages` computes for the persisted case, generalized
    here to Preview's zero-write, twice-per-affected-Product (baseline/projected)
    shape. Baseline and projected must each get their OWN combined read — never
    mixed — since they represent two different hypothetical worlds."""
    ingredient_ids: set[uuid.UUID] = set()
    exclude_requirement_ids: set[uuid.UUID] = set()
    extra_candidate: dict[uuid.UUID, Decimal] = {}
    for result in results:
        ingredient_ids |= set(result.candidate_ingredient_totals)
        exclude_requirement_ids |= result.rebuildable_requirement_ids
        for ingredient_id, quantity in result.candidate_ingredient_totals.items():
            extra_candidate[ingredient_id] = (
                extra_candidate.get(ingredient_id, Decimal(0)) + quantity
            )
    return compute_combined_ingredient_shortages(
        db,
        business,
        ingredient_ids,
        exclude_requirement_ids=exclude_requirement_ids,
        extra_candidate=extra_candidate,
    )


def _gather_confirmed_custom_item_lines(
    db: Session, business: Business, *, exclude_order_id: uuid.UUID | None
) -> list[demand_aggregation.CustomItemWorkloadLine]:
    """Confirmed-world baseline Custom Item manual workload (Phase 7 Final
    Remediation Correction Plan, Finding 6) — every CONFIRMED Order's own
    CUSTOM_ITEM line with a recorded `custom_active_time_minutes`. Before this,
    `preview_order` built `custom_item_workload` exclusively from `payload.lines`
    (hypothetical-only) — there was no "confirmed world" computation to compare
    against at all, unlike Produced/Purchased demand's baseline/projected split.
    Mirrors the same non-misattribution principle Finding 2/amendment 2 already
    established for Produced demand: a CONFIRMED-edit preview's baseline must
    exclude THIS Order's own persisted contribution (`exclude_order_id`) — it is
    about to be replaced by the hypothetical payload — never a sibling Order's
    still-live one."""
    conditions = [
        Order.business_id == business.id,
        Order.status == OrderStatus.CONFIRMED,
        Order.fulfillment_date.is_not(None),
        OrderLine.business_id == business.id,
        OrderLine.line_type == OrderLineType.CUSTOM_ITEM,
        OrderLine.custom_active_time_minutes.is_not(None),
    ]
    if exclude_order_id is not None:
        conditions.append(Order.id != exclude_order_id)
    rows = db.execute(
        select(OrderLine, Order).join(Order, OrderLine.order_id == Order.id).where(*conditions)
    ).all()
    return [
        demand_aggregation.CustomItemWorkloadLine(
            order_id=order.id,
            order_line_id=line.id,
            demand_date=order.fulfillment_date,
            custom_active_time_minutes=line.custom_active_time_minutes,
        )
        for line, order in rows
    ]


class PreviewResult:
    def __init__(
        self,
        *,
        subtotal: Decimal,
        final_total: Decimal,
        warning_issues: list[dict],
        warning_fingerprints: set[str],
        requirement_previews: list[ProductionRequirementPreview],
        ingredient_availability: list[IngredientAvailabilityPreview],
        purchased_shortages: list[PurchasedShortagePreview],
        custom_item_workload: list[CustomItemWorkloadPreview],
        fulfillment_date_required_for_operational_preview: bool,
    ) -> None:
        self.subtotal = subtotal
        self.final_total = final_total
        self.warning_issues = warning_issues
        self.warning_fingerprints = warning_fingerprints
        self.requirement_previews = requirement_previews
        self.ingredient_availability = ingredient_availability
        self.purchased_shortages = purchased_shortages
        self.custom_item_workload = custom_item_workload
        self.fulfillment_date_required_for_operational_preview = (
            fulfillment_date_required_for_operational_preview
        )


def preview_order(
    db: Session,
    business: Business,
    payload: OrderCreateRequest,
    *,
    order_id: uuid.UUID | None = None,
    business_today: date,
) -> PreviewResult:
    """Preview for a brand-new unsaved Order (`order_id=None`), unsaved edits to a
    persisted DRAFT Order, or a hypothetical edit to a persisted CONFIRMED Order —
    see module docstring for the exact composition rule each case uses."""
    order: Order | None = None
    exclude_order_id: uuid.UUID | None = None
    existing_lines_by_id: dict[uuid.UUID, OrderLine] = {}
    protected_line_ids: frozenset[uuid.UUID] = frozenset()
    if order_id is not None:
        order = order_service.get_order_for_business(db, order_id, business)
        # Phase 7 Final Lifecycle Invariant Correction Plan, Finding B — makes the
        # Preview lifecycle contract explicit: DRAFT and CONFIRMED are the only two
        # previewable statuses. READY/COMPLETED/CANCELED are rejected outright; no
        # READY/COMPLETED *behavior* is implemented here, this is solely an API
        # guard. Runs before any lock/mutation has happened, so no rollback is
        # needed for this rejection.
        if order.status not in (OrderStatus.DRAFT, OrderStatus.CONFIRMED):
            raise ApiError(
                409,
                "ORDER_NOT_PREVIEWABLE",
                "Preview is not available for an order in this status.",
            )
        existing_lines_by_id = {line.id: line for line in order.lines}
        if order.status is OrderStatus.CONFIRMED:
            exclude_order_id = order.id

    # Phase 7 Final Semantic & Precision Correction Plan, Finding 5 — a missing
    # Draft fulfillment date must never be silently treated as "today". No
    # operational demand (production requirements, ingredient reservations, dated
    # Custom Item workload) is fabricated for an invented date; only the
    # financial subtotal/final total remain available.
    has_fulfillment_date = payload.fulfillment_date is not None

    # Phase 7 Final Lifecycle Invariant Correction Plan, Finding C — a supplied
    # fulfillment date+time must be a valid, unambiguous Business-local moment,
    # applied uniformly to all three Preview cases (new Order, DRAFT edit,
    # CONFIRMED edit) regardless of status or Product-type composition. No
    # lock/mutation has happened yet at this point, so no rollback is needed for
    # this rejection either.
    validate_fulfillment_local_time(business, payload.fulfillment_date, payload.fulfillment_time)

    try:
        if order is not None and order.status is OrderStatus.CONFIRMED:
            # Phase 7 Final Lifecycle Invariant Correction Plan, Finding B — the
            # structural check must run BEFORE the active-run/protected-demand
            # check, mirroring `update_confirmed_order`'s own corrected ordering:
            # a structurally invalid candidate (e.g. `lines=[]`) is rejected as
            # `ORDER_NOT_CONFIRMABLE`, never masked by `ORDER_PRODUCTION_LOCKED`.
            # Runs unconditionally (not nested inside the protected-line check
            # below), so it still fires even when there are zero protected lines.
            issues = order_pricing.check_candidate_structural_readiness(
                line_count=len(payload.lines),
                fulfillment_date_present=payload.fulfillment_date is not None,
            )
            if issues:
                raise _structural_readiness_error(issues)

            # Phase 7 Final Semantic & Precision Correction Plan, Finding 4 — the
            # exact same precondition function the real confirmed-edit path uses
            # (`order_lifecycle_service._reject_if_confirmed_edit_touches_protected_
            # demand`), reused unchanged rather than duplicated, so Preview and a
            # real save reach identical outcomes by construction. A retained
            # protected line whose operational dimensions are unchanged is left as
            # fixed state below (never re-added as hypothetical rebuildable
            # demand); any proposal that would actually alter protected demand
            # raises the identical `ORDER_PRODUCTION_LOCKED` conflict a real save
            # would.
            protected_line_ids = frozenset(
                get_order_production_locked_line_ids(db, business, order)
            )
            if protected_line_ids:
                _reject_if_confirmed_edit_touches_protected_demand(db, business, order, payload)

        old_product_ids = frozenset(
            line.product_id
            for line in (order.lines if order is not None else [])
            if line.product_id is not None
        )
        new_product_ids = frozenset(
            line.product_id for line in payload.lines if line.product_id is not None
        )
        additional_product_ids = old_product_ids | new_product_ids

        _customer, products_by_id, selling_options_by_id = (
            order_service._validate_and_lock_order_references(
                db,
                business,
                customer_id=payload.customer_id,
                existing_customer_id=order.customer_id if order is not None else None,
                lines=payload.lines,
                existing_lines_by_id=existing_lines_by_id,
                additional_product_ids=additional_product_ids,
                lock=False,
            )
        )

        synthetic_order_id = order.id if order is not None else uuid.uuid4()
        line_subtotals: list[Decimal] = []
        hypothetical_by_product: dict[uuid.UUID, list[HypotheticalLine]] = {}
        custom_item_lines: list[demand_aggregation.CustomItemWorkloadLine] = []
        for line in payload.lines:
            existing = existing_lines_by_id.get(line.id) if line.id is not None else None
            columns = order_service._reconcile_line(
                line, existing, products_by_id, selling_options_by_id
            )
            if columns is not None and "line_subtotal" in columns:
                line_subtotal = columns["line_subtotal"]
                underlying_quantity = columns.get("underlying_quantity")
            else:
                # Class A (unrelated edit to a retained line, Final Plan §D.2) — no
                # new snapshot; the line's own already-stored values still
                # contribute to the preview exactly as they would to a real save.
                # A notes-only Class B edit (`_reconcile_line` returning a PARTIAL
                # `{"notes": ...}` dict rather than a full snapshot — pre-existing
                # `order_service` behavior, unrelated to this round's findings)
                # takes this same branch: notes never affect pricing/quantity, so
                # the line's own already-stored subtotal/quantity still apply.
                assert existing is not None
                line_subtotal = existing.line_subtotal
                underlying_quantity = existing.underlying_quantity
            line_subtotals.append(line_subtotal)

            # A retained line keeps its own stable, real order_line_id so
            # `recalculate_product_closure`'s pin-reuse logic (`_resolve_pin`)
            # applies to it exactly as a real edit would (Amendment §4) — only a
            # genuinely new line gets a synthetic, never-persisted id.
            stable_line_id = line.id if line.id is not None else uuid.uuid4()

            # Finding 4 — a retained protected line whose operational dimensions
            # are unchanged (guaranteed by the precondition check above) is left
            # as fixed state: it is NOT re-added as hypothetical rebuildable
            # demand, since its real, protected `ProductionRequirement` already
            # represents it correctly. Its financial contribution above
            # (`line_subtotals.append`) is unconditional and still applies.
            is_retained_protected_line = line.id is not None and line.id in protected_line_ids

            if (
                not is_retained_protected_line
                and has_fulfillment_date
                and line.line_type is not OrderLineType.CUSTOM_ITEM
                and line.product_id is not None
            ):
                hypothetical_by_product.setdefault(line.product_id, []).append(
                    HypotheticalLine(
                        order_id=synthetic_order_id,
                        order_line_id=stable_line_id,
                        product_id=line.product_id,
                        underlying_quantity=underlying_quantity,
                        fulfillment_date=payload.fulfillment_date,
                        fulfillment_time=payload.fulfillment_time,
                    )
                )
            elif (
                has_fulfillment_date
                and line.line_type is OrderLineType.CUSTOM_ITEM
                and line.custom_active_time_minutes is not None
            ):
                # Custom Item manual workload (Finding 5) — never routed through
                # `recalculate_product_closure`: ORD-009/010, a Custom Item line
                # always has `product_id = NULL` and
                # `production_requirements.product_id` is `NOT NULL`.
                custom_item_lines.append(
                    demand_aggregation.CustomItemWorkloadLine(
                        order_id=synthetic_order_id,
                        order_line_id=stable_line_id,
                        demand_date=payload.fulfillment_date,
                        custom_active_time_minutes=line.custom_active_time_minutes,
                    )
                )

        totals = order_pricing.calculate_order_totals(
            line_subtotals,
            order_adjustment=order_pricing.quantize_money(payload.order_adjustment),
            manual_tax=order_pricing.quantize_money(payload.manual_tax),
        )

        baseline_results: list[RecalcResult] = []
        projected_results: list[RecalcResult] = []
        requirement_previews: list[ProductionRequirementPreview] = []
        purchased_shortage_previews: list[PurchasedShortagePreview] = []

        for product_id in sorted(additional_product_ids):
            product = products_by_id.get(product_id)
            if product is None:
                continue

            baseline_result = recalculate_product_closure(
                db,
                business,
                product,
                business_today=business_today,
                exclude_order_id=exclude_order_id,
                hypothetical_lines=(),
                persist=False,
            )
            projected_result = recalculate_product_closure(
                db,
                business,
                product,
                business_today=business_today,
                exclude_order_id=exclude_order_id,
                hypothetical_lines=hypothetical_by_product.get(product_id, []),
                persist=False,
            )
            baseline_results.append(baseline_result)
            projected_results.append(projected_result)

            baseline_by_key = {
                (r.recipe_revision_id, r.demand_date): r for r in baseline_result.requirements
            }
            projected_by_key = {
                (r.recipe_revision_id, r.demand_date): r for r in projected_result.requirements
            }
            for key in sorted(
                set(baseline_by_key) | set(projected_by_key),
                key=lambda k: (k[1], str(k[0])),
            ):
                revision_id, demand_date = key
                baseline_summary = baseline_by_key.get(key)
                projected_summary = projected_by_key.get(key)
                if baseline_summary is None:
                    assert projected_summary is not None
                    baseline_summary = _zeroed_summary(projected_summary, product_id=product_id)
                if projected_summary is None:
                    projected_summary = _zeroed_summary(baseline_summary, product_id=product_id)
                requirement_previews.append(
                    ProductionRequirementPreview(
                        product_id=product_id,
                        recipe_revision_id=revision_id,
                        demand_date=demand_date,
                        is_protected=projected_summary.is_protected,
                        missing_recipe=projected_summary.missing_recipe,
                        baseline=baseline_summary,
                        projected=projected_summary,
                    )
                )

            if (
                baseline_result.purchased_shortage_quantity is not None
                or projected_result.purchased_shortage_quantity is not None
            ):
                purchased_shortage_previews.append(
                    PurchasedShortagePreview(
                        product_id=product_id,
                        baseline_shortage_quantity=(
                            baseline_result.purchased_shortage_quantity or _ZERO
                        ),
                        projected_shortage_quantity=(
                            projected_result.purchased_shortage_quantity or _ZERO
                        ),
                    )
                )

        # Combined shared-Ingredient shortage math (Phase 7 Final Remediation
        # Correction Plan, Finding 5): each affected Product's own
        # `recalculate_product_closure(persist=False)` call is fully independent —
        # a `persist=False` pass never flushes, so one Product's own hypothetical
        # candidate reservation for a shared Ingredient is invisible to a sibling
        # Product's own call in this same request. Reading the combined dict once,
        # separately for the baseline set and the projected set, is the fix (Final
        # Architecture Lock §E's formula, extended to the multi-Product case).
        baseline_combined_shortages = _combined_shortages_for_results(
            db, business, baseline_results
        )
        projected_combined_shortages = _combined_shortages_for_results(
            db, business, projected_results
        )
        ingredient_availability_by_id: dict[uuid.UUID, IngredientAvailabilityPreview] = {}
        for ingredient_id in sorted(
            set(baseline_combined_shortages) | set(projected_combined_shortages),
            key=str,
        ):
            baseline_shortage = baseline_combined_shortages.get(ingredient_id)
            projected_shortage = projected_combined_shortages.get(ingredient_id)
            authoritative_shortage = (
                projected_shortage if projected_shortage is not None else baseline_shortage
            )
            ingredient_availability_by_id[ingredient_id] = IngredientAvailabilityPreview(
                ingredient_id=ingredient_id,
                ingredient_name=authoritative_shortage.ingredient_name,
                canonical_unit=authoritative_shortage.canonical_unit,
                physical_quantity=authoritative_shortage.physical_quantity,
                baseline_shortage_quantity=(
                    baseline_shortage.shortage_quantity if baseline_shortage is not None else _ZERO
                ),
                projected_shortage_quantity=(
                    projected_shortage.shortage_quantity
                    if projected_shortage is not None
                    else _ZERO
                ),
            )

        missing_fulfillment_time = payload.fulfillment_time is None
        warning_fingerprints = _collect_warning_fingerprints(
            projected_results,
            missing_fulfillment_time=missing_fulfillment_time,
            combined_ingredient_shortages=projected_combined_shortages,
        )
        warning_issues = _warning_issues(
            projected_results,
            missing_fulfillment_time=missing_fulfillment_time,
            combined_ingredient_shortages=projected_combined_shortages,
        )
        # Custom Item manual workload baseline/projected split (Finding 6):
        # `custom_item_lines` above is only ever this request's HYPOTHETICAL
        # Custom Item lines — the confirmed-world baseline comes from a real
        # query, excluding this Order's own persisted contribution for a
        # CONFIRMED-edit preview exactly as the Produced/Purchased baseline does.
        baseline_custom_item_lines = _gather_confirmed_custom_item_lines(
            db, business, exclude_order_id=exclude_order_id
        )
        projected_custom_item_lines = baseline_custom_item_lines + custom_item_lines
        baseline_workload_by_date = {
            w.demand_date: w
            for w in demand_aggregation.aggregate_custom_item_workload(baseline_custom_item_lines)
        }
        projected_workload_by_date = {
            w.demand_date: w
            for w in demand_aggregation.aggregate_custom_item_workload(projected_custom_item_lines)
        }
        custom_item_workload = [
            CustomItemWorkloadPreview(
                demand_date=demand_date,
                baseline_total_active_minutes=(
                    baseline_workload_by_date[demand_date].total_active_minutes
                    if demand_date in baseline_workload_by_date
                    else 0
                ),
                projected_total_active_minutes=(
                    projected_workload_by_date[demand_date].total_active_minutes
                    if demand_date in projected_workload_by_date
                    else 0
                ),
                baseline_contributing_order_line_ids=(
                    baseline_workload_by_date[demand_date].contributing_order_line_ids
                    if demand_date in baseline_workload_by_date
                    else ()
                ),
                projected_contributing_order_line_ids=(
                    projected_workload_by_date[demand_date].contributing_order_line_ids
                    if demand_date in projected_workload_by_date
                    else ()
                ),
            )
            for demand_date in sorted(
                set(baseline_workload_by_date) | set(projected_workload_by_date)
            )
        ]
    finally:
        # Zero-write guarantee: every lock acquired above is released here,
        # success or failure alike — Preview never commits.
        db.rollback()

    return PreviewResult(
        subtotal=order_pricing.quantize_money(totals.subtotal),
        final_total=order_pricing.quantize_money(totals.final_total),
        warning_issues=warning_issues,
        warning_fingerprints=warning_fingerprints,
        requirement_previews=requirement_previews,
        ingredient_availability=sorted(
            ingredient_availability_by_id.values(), key=lambda a: str(a.ingredient_id)
        ),
        purchased_shortages=purchased_shortage_previews,
        custom_item_workload=custom_item_workload,
        fulfillment_date_required_for_operational_preview=not has_fulfillment_date,
    )
