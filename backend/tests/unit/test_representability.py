"""Unit tests for the Phase 7 INT32 representability guard (Final Architecture Lock
§G) and a layering test proving Phase 7 pure `app/domain/` modules never import
`ApiError` (Final Architecture Lock §G: "Do not import ApiError into a pure calculator
module")."""

from __future__ import annotations

import ast
from pathlib import Path

from app.domain.representability import INT32_MAX, INT32_MIN, is_representable_in_int32

_PHASE_7_DOMAIN_MODULES = [
    "demand_aggregation.py",
    "surplus_allocation.py",
    "ingredient_requirements.py",
    "production_costing.py",
    "representability.py",
]


def test_int32_bounds():
    assert is_representable_in_int32(0)
    assert is_representable_in_int32(INT32_MAX)
    assert is_representable_in_int32(INT32_MIN)
    assert not is_representable_in_int32(INT32_MAX + 1)
    assert not is_representable_in_int32(INT32_MIN - 1)


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_phase_7_domain_modules_never_import_api_error():
    domain_dir = Path(__file__).resolve().parents[2] / "app" / "domain"
    for filename in _PHASE_7_DOMAIN_MODULES:
        source = (domain_dir / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        imported = _imported_names(tree)
        assert not any("api_errors" in name or "ApiError" in name for name in imported), (
            f"{filename} must not import ApiError/api_errors (pure domain layer)"
        )
