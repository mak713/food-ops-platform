"""order_lines.line_type/product_id/selling_option_id shape (Phase 1 plan §4.14).

STANDARD_OPTION requires both product_id and selling_option_id.
CUSTOM_QUANTITY requires product_id, forbids selling_option_id.
CUSTOM_ITEM requires both product_id and selling_option_id to be NULL.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.enums import OrderLineType
from tests.integration.schema import factories as f


def _setup(session):
    business = f.make_business_graph(session)
    product = f.make_product(session, business)
    session.flush()
    selling_option = f.make_selling_option(session, business, product)
    order = f.make_order(session, business)
    session.flush()
    return business, product, selling_option, order


def test_standard_option_requires_both_product_and_selling_option(session):
    business, product, selling_option, order = _setup(session)

    f.make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.STANDARD_OPTION,
        product=product,
        selling_option=selling_option,
    )
    session.flush()  # valid combination succeeds

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.STANDARD_OPTION,
                product=product,
                selling_option=None,
            )
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.STANDARD_OPTION,
                product=None,
                selling_option=selling_option,
            )
            session.flush()


def test_custom_quantity_requires_product_forbids_selling_option(session):
    business, product, selling_option, order = _setup(session)

    f.make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_QUANTITY,
        product=product,
        selling_option=None,
    )
    session.flush()  # valid combination succeeds

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=product,
                selling_option=selling_option,
            )
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.CUSTOM_QUANTITY,
                product=None,
                selling_option=None,
            )
            session.flush()


def test_custom_item_requires_both_null(session):
    business, product, selling_option, order = _setup(session)

    f.make_order_line(
        session,
        business,
        order,
        line_type=OrderLineType.CUSTOM_ITEM,
        product=None,
        selling_option=None,
    )
    session.flush()  # valid combination succeeds

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.CUSTOM_ITEM,
                product=None,
                selling_option=selling_option,
            )
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            f.make_order_line(
                session,
                business,
                order,
                line_type=OrderLineType.CUSTOM_ITEM,
                product=product,
                selling_option=None,
            )
            session.flush()
