from sqlalchemy import Numeric

# Precision rules (Phase 1 plan §1.4):
#   MONEY    — customer-facing amounts and final ledger entries.
#   QUANTITY — physical quantities and unit-cost-basis fields, plus internal
#              (non-customer-facing) cost aggregates computed from them.
#   RATIO    — margins/percentages, stored as e.g. 0.60 for 60%.
MONEY = Numeric(14, 2)
QUANTITY = Numeric(18, 6)
RATIO = Numeric(9, 6)
