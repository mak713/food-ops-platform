"""Deterministic Analytics fixtures (Spec §19.12). Completed Orders are inserted with exact
`completed_at` instants and explicit immutable ledger rows of every cost type, so every
expected figure in the Analytics tests is a hand-computed constant — never derived from the
code under test. The `Golden` scenario is documented inline with its arithmetic.

All timestamps are UTC; the Business timezone is `America/Chicago` (CDT, UTC-5, in October
2026), so ``2026-10-17T04:59:59Z`` is Oct 16 23:59:59 local and ``2026-10-17T05:00:00Z`` is
Oct 17 00:00:00 local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.api.v1 import analytics as analytics_router
from app.db.enums import (
    CostType,
    FulfillmentMethod,
    OrderLineType,
    OrderStatus,
    ProductionRunStatus,
    ProductType,
)
from app.db.models.business import Business
from app.db.models.customer import Customer
from app.db.models.order import Order, OrderLine
from app.db.models.product import Product, SellingOption
from tests.integration.schema.factories import (
    make_customer,
    make_order,
    make_order_cost_allocation,
    make_order_line,
    make_payment,
    make_product,
    make_production_run,
    make_production_run_order_allocation,
    make_purchased_inventory_transaction,
    make_recipe,
    make_recipe_revision,
    make_selling_option,
    make_surplus_inventory,
)

TIMEZONE = "America/Chicago"
NOW = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)  # 13:00 CDT, Fri Oct 16 2026
TODAY = date(2026, 10, 16)


def at(day: int, hour: int = 12, minute: int = 0, second: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


def freeze(monkeypatch, when: datetime = NOW) -> None:
    monkeypatch.setattr(analytics_router, "_utc_now", lambda: when)


D = Decimal


@dataclass
class Line:
    """Describes one Order Line plus the historical cost rows written for it."""

    product: Product | None
    kind: OrderLineType
    subtotal: str
    units: str = "1"
    packages: str = "1"
    option: SellingOption | None = None
    costs: dict[CostType, str] = field(default_factory=dict)
    name: str = "Line"


class Builder:
    def __init__(self, session: Session, business: Business) -> None:
        self.session = session
        self.business = business
        self._runs: dict = {}

    # ---- master data
    def product(self, name: str, ptype: ProductType = ProductType.PRODUCED) -> Product:
        p = make_product(self.session, self.business, ptype, name=name)
        self.session.flush()
        return p

    def option(self, product: Product, name: str, units: str, price: str) -> SellingOption:
        o = make_selling_option(
            self.session,
            self.business,
            product,
            name=name,
            quantity_units=D(units),
            price=D(price),
        )
        self.session.flush()
        return o

    def customer(self, name: str) -> Customer:
        c = make_customer(self.session, self.business, name=name)
        self.session.flush()
        return c

    # ---- orders
    def _run_for(self, product: Product):
        if product.id not in self._runs:
            recipe = make_recipe(self.session, self.business, product)
            revision = make_recipe_revision(self.session, self.business, recipe)
            self.session.flush()
            run = make_production_run(
                self.session,
                self.business,
                product,
                revision,
                status=ProductionRunStatus.COMPLETED,
            )
            self.session.flush()
            self._runs[product.id] = run
        return self._runs[product.id]

    def _write_costs(self, order: Order, line: OrderLine, spec: Line) -> None:
        for cost_type, amount in spec.costs.items():
            kwargs: dict = {"order_line_id": line.id, "cost_type": cost_type, "amount": D(amount)}
            if cost_type is CostType.PRODUCTION:
                assert spec.product is not None
                alloc = make_production_run_order_allocation(
                    self.session, self.business, self._run_for(spec.product), order, line
                )
                self.session.flush()
                kwargs["production_run_order_allocation_id"] = alloc.id
            elif cost_type is CostType.SURPLUS:
                assert spec.product is not None
                lot = make_surplus_inventory(self.session, self.business, spec.product)
                self.session.flush()
                kwargs["surplus_inventory_id"] = lot.id
            elif cost_type is CostType.PURCHASED_GOOD:
                assert spec.product is not None
                tx = make_purchased_inventory_transaction(self.session, self.business, spec.product)
                self.session.flush()
                kwargs["purchased_inventory_transaction_id"] = tx.id
            make_order_cost_allocation(self.session, self.business, order, **kwargs)

    def order(
        self,
        *,
        status: OrderStatus = OrderStatus.COMPLETED,
        completed_at: datetime | None = None,
        canceled_at: datetime | None = None,
        customer: Customer | None = None,
        method: FulfillmentMethod | None = FulfillmentMethod.PICKUP,
        lines: list[Line],
        adjustment: str = "0.00",
        tax: str = "0.00",
        payments: list[str] | None = None,
        number: str | None = None,
    ) -> Order:
        kwargs = {"status": status, "customer_id": customer.id if customer else None}
        if number is not None:
            kwargs["order_number"] = number
        order = make_order(self.session, self.business, **kwargs)
        subtotal = sum((D(spec.subtotal) for spec in lines), D("0.00"))
        order.subtotal = subtotal
        order.order_adjustment = D(adjustment)
        if D(adjustment) != 0:
            order.adjustment_description = "Adjustment"
        order.manual_tax = D(tax)
        order.final_total = subtotal + D(adjustment) + D(tax)
        order.fulfillment_method = method
        order.completed_at = completed_at
        order.canceled_at = canceled_at
        self.session.flush()
        for spec in lines:
            line = make_order_line(
                self.session,
                self.business,
                order,
                line_type=spec.kind,
                product=spec.product,
                selling_option=spec.option,
                display_name_snapshot=spec.name,
                package_quantity=D(spec.packages),
                underlying_quantity=D(spec.units),
                line_subtotal=D(spec.subtotal),
                charged_unit_price_snapshot=D(spec.subtotal),
            )
            self.session.flush()
            self._write_costs(order, line, spec)
        for amount in payments or []:
            make_payment(self.session, self.business, order, amount=D(amount))
        self.session.flush()
        return order


def std(product, option, packages, units, subtotal, costs=None) -> Line:
    return Line(
        product=product,
        kind=OrderLineType.STANDARD_OPTION,
        subtotal=subtotal,
        units=units,
        packages=packages,
        option=option,
        costs=costs or {},
        name=f"{product.name} — {option.name}",
    )


def custom_qty(product, units, subtotal, costs=None) -> Line:
    return Line(
        product=product,
        kind=OrderLineType.CUSTOM_QUANTITY,
        subtotal=subtotal,
        units=units,
        packages="1",
        costs=costs or {},
        name=product.name,
    )


def custom_item(subtotal, costs=None, units="1") -> Line:
    return Line(
        product=None,
        kind=OrderLineType.CUSTOM_ITEM,
        subtotal=subtotal,
        units=units,
        costs=costs or {},
        name="Gift tag",
    )


P = CostType.PRODUCTION
S = CostType.SURPLUS
G = CostType.PURCHASED_GOOD
K = CostType.PACKAGING
C = CostType.CUSTOM_DIRECT


@dataclass
class Golden:
    """The deterministic reference scenario (Business timezone America/Chicago).

    Selected period for most tests: ``LAST_7_DAYS`` on 2026-10-16 = Oct 10..Oct 16
    (UTC ``[2026-10-10T05:00Z, 2026-10-17T05:00Z)``); previous = Oct 3..Oct 9.

    In-period Completed Orders (final_total / ledger cost):
      O1  Alice  Oct12 15:00Z  PICKUP    46.00 / 12.75   (tax 2.00; paid 46.00)
      O2  Bob    Oct13 01:30Z  DELIVERY  72.00 / 21.50   (adjustment -5.00; paid 40.00)
      O3  Guest  Oct14 20:00Z  PICKUP    17.00 /  2.70   (no payments)
      O4  Alice  Oct15 14:00Z  DELIVERY  24.00 /  6.60   (two Cookie 6-pack lines; paid 30.00)
      O5  Cara   Oct16 16:00Z  none       0.00 /  2.00   ($0 Order)
      E1  Bob    Oct17 04:59:59Z PICKUP    3.00 /  0.50   (Oct 16 23:59:59 local: IN)
      E2  Guest  Oct10 05:00:00Z OTHER     4.00 /  1.00   (Oct 10 00:00:00 local: IN)
    Revenue 166.00; Direct Cost 47.05; Contribution 118.95; 7 Orders.
    Previous-period Completed Orders:
      O6  Cara   Oct05 15:00Z   30.00 / 10.00
      E3  Guest  Oct10 04:59:59Z  9.00 /  1.00   (Oct 9 23:59:59 local: previous period)
    Previous: Revenue 39.00; Direct Cost 11.00; 2 Orders.
    Never counted: E4 (Oct17 05:00:00Z, 100.00/50.00; next local day), a CONFIRMED Order
    with a PRODUCTION cost row, a Ready and a Draft Order, and two Canceled Orders.
    """

    builder: Builder
    alice: Customer
    bob: Customer
    cara: Customer
    cookie: Product
    brownie: Product
    lemonade: Product
    six_pack: SellingOption
    twelve_pack: SellingOption
    bottle: SellingOption
    orders: dict[str, Order]


def build_golden(session: Session, business: Business) -> Golden:
    b = Builder(session, business)
    alice, bob, cara = b.customer("Alice"), b.customer("Bob"), b.customer("Cara")
    cookie = b.product("Cookie")
    brownie = b.product("Brownie")
    lemonade = b.product("Lemonade", ProductType.PURCHASED)
    six = b.option(cookie, "6-pack", "6", "12.00")
    twelve = b.option(cookie, "12-pack", "12", "22.00")
    bottle = b.option(lemonade, "Bottle", "1", "3.00")
    o: dict[str, Order] = {}

    o["O1"] = b.order(
        completed_at=at(12, 15),
        customer=alice,
        method=FulfillmentMethod.PICKUP,
        tax="2.00",
        payments=["46.00"],
        number="O1",
        lines=[
            std(cookie, six, "2", "12", "24.00", {P: "6.00", K: "0.50"}),
            custom_qty(brownie, "8", "20.00", {P: "5.00", S: "1.25", K: "0.00"}),
        ],
    )
    o["O2"] = b.order(
        completed_at=at(13, 1, 30),
        customer=bob,
        method=FulfillmentMethod.DELIVERY,
        adjustment="-5.00",
        payments=["40.00"],
        number="O2",
        lines=[
            std(cookie, twelve, "1", "12", "22.00", {P: "7.10", K: "0.40"}),
            custom_qty(cookie, "30", "55.00", {P: "14.00", K: "0.00"}),
        ],
    )
    o["O3"] = b.order(
        completed_at=at(14, 20),
        customer=None,
        method=FulfillmentMethod.PICKUP,
        number="O3",
        lines=[
            std(lemonade, bottle, "4", "4", "12.00", {G: "1.00", K: "0.20"}),
            custom_item("5.00", {C: "1.50", K: "0.00"}),
        ],
    )
    o["O4"] = b.order(
        completed_at=at(15, 14),
        customer=alice,
        method=FulfillmentMethod.DELIVERY,
        payments=["30.00"],
        number="O4",
        lines=[
            std(cookie, six, "1", "6", "12.00", {P: "3.05", K: "0.25"}),
            std(cookie, six, "1", "6", "12.00", {P: "3.05", K: "0.25"}),
        ],
    )
    o["O5"] = b.order(
        completed_at=at(16, 16),
        customer=cara,
        method=None,
        number="O5",
        lines=[custom_qty(brownie, "4", "0.00", {P: "2.00"})],
    )
    o["E1"] = b.order(
        completed_at=at(17, 4, 59, 59),
        customer=bob,
        method=FulfillmentMethod.PICKUP,
        number="E1",
        lines=[custom_qty(brownie, "1", "3.00", {K: "0.50"})],
    )
    o["E2"] = b.order(
        completed_at=at(10, 5, 0, 0),
        customer=None,
        method=FulfillmentMethod.OTHER,
        number="E2",
        lines=[custom_qty(brownie, "2", "4.00", {K: "1.00"})],
    )
    # previous period (Oct 3..Oct 9)
    o["O6"] = b.order(
        completed_at=at(5, 15),
        customer=cara,
        number="O6",
        lines=[custom_qty(brownie, "10", "30.00", {P: "9.00", K: "1.00"})],
    )
    o["E3"] = b.order(
        completed_at=at(10, 4, 59, 59),
        customer=None,
        number="E3",
        lines=[custom_qty(brownie, "3", "9.00", {K: "1.00"})],
    )
    # never counted in the selected period
    o["E4"] = b.order(
        completed_at=at(17, 5, 0, 0),
        customer=bob,
        number="E4",
        lines=[custom_qty(brownie, "5", "100.00", {K: "50.00"})],
    )
    o["CONFIRMED"] = b.order(
        status=OrderStatus.CONFIRMED,
        customer=alice,
        number="CONF",
        lines=[custom_qty(cookie, "12", "40.00", {P: "8.00"})],
    )
    o["READY"] = b.order(
        status=OrderStatus.READY,
        customer=alice,
        number="READY",
        lines=[custom_qty(cookie, "12", "41.00", {P: "8.00"})],
    )
    o["DRAFT"] = b.order(
        status=OrderStatus.DRAFT,
        customer=bob,
        number="DRAFT",
        lines=[custom_qty(cookie, "12", "42.00")],
    )
    o["CANCELED_IN"] = b.order(
        status=OrderStatus.CANCELED,
        canceled_at=at(11, 12),
        customer=bob,
        number="CANC-IN",
        lines=[custom_qty(cookie, "24", "77.00", {P: "99.00"})],
    )
    o["CANCELED_OUT"] = b.order(
        status=OrderStatus.CANCELED,
        canceled_at=at(1, 12),
        customer=bob,
        number="CANC-OUT",
        lines=[custom_qty(cookie, "24", "70.00")],
    )
    session.commit()
    return Golden(b, alice, bob, cara, cookie, brownie, lemonade, six, twelve, bottle, o)


def other_tenant_noise(session: Session, business: Business) -> None:
    """A second tenant's Completed Orders on overlapping dates, to prove isolation."""
    b = Builder(session, business)
    cust = b.customer("Alice")  # same display name, different tenant
    prod = b.product("Cookie")
    b.order(
        completed_at=at(12, 15),
        customer=cust,
        number="X1",
        lines=[custom_qty(prod, "10", "1000.00", {P: "400.00", K: "1.00"})],
    )
    b.order(
        completed_at=at(13, 15),
        customer=None,
        number="X2",
        lines=[custom_qty(prod, "10", "500.00", {K: "5.00"})],
    )
    session.commit()


def setup_golden(client, session: Session, monkeypatch) -> Golden:
    """Signs up a Chicago-timezone Business, freezes the clock at `NOW`, builds `Golden`."""
    from tests.integration.api.helpers import signup
    from tests.integration.api.phase9_support import business_by_name

    signup(client, business_timezone=TIMEZONE)
    business = business_by_name(session)
    freeze(monkeypatch)
    return build_golden(session, business)
