"""The dashboard may only show what an agent actually received.

The belief panels exist to demonstrate that each UAV acts on partial, delayed,
sometimes wrong information. That demonstration is worthless if the renderer
quietly reaches for world truth to fill a gap, so this is enforced structurally
rather than left as an intention: the dashboard is allowed to import the trace
and nothing else from the package.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "eudis_swarm"

DASHBOARD_MODULES = ("dashboard.py", "dashboard_app.py")

#: Names that carry authoritative world state. A dashboard that imports any of
#: these can read ground truth instead of per-agent evidence.
FORBIDDEN_MODULES = frozenset(
    {
        "eudis_swarm.mission",
        "eudis_swarm.simulation",
        "eudis_swarm.agent",
        "eudis_swarm.communication",
        "eudis_swarm.metrics",
        "eudis_swarm.evaluation",
        "eudis_swarm.peer_state",
        "eudis_swarm.task_claims",
        "mission",
        "simulation",
        "agent",
        "communication",
        "metrics",
        "evaluation",
        "peer_state",
        "task_claims",
    }
)

FORBIDDEN_NAMES = frozenset({"Mission", "Simulation", "Agent", "OwnershipEvaluator"})


def _imported_modules(source: str) -> set[str]:
    """Collect every module a file imports, absolute and relative alike."""

    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.add(node.module)
            if node.level:
                # a relative import such as `from .mission import Mission`
                modules.add(node.module or "")
    return modules


def _imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module_name", DASHBOARD_MODULES)
def test_the_dashboard_cannot_import_world_truth(module_name: str) -> None:
    source = (SOURCE_ROOT / module_name).read_text(encoding="utf-8")

    offending = _imported_modules(source) & FORBIDDEN_MODULES

    assert not offending, (
        f"{module_name} imports {sorted(offending)}, which carries authoritative "
        "world state. The dashboard must reconstruct belief from the trace only."
    )


@pytest.mark.parametrize("module_name", DASHBOARD_MODULES)
def test_the_dashboard_cannot_name_authoritative_types(module_name: str) -> None:
    source = (SOURCE_ROOT / module_name).read_text(encoding="utf-8")

    offending = _imported_names(source) & FORBIDDEN_NAMES

    assert not offending, (
        f"{module_name} imports {sorted(offending)}. Rendering must use the "
        "recorded per-agent trace fields, never a live authoritative object."
    )


def test_the_guard_would_catch_a_real_violation() -> None:
    """A test that cannot fail is not a guard; prove this one bites."""

    violation = "from eudis_swarm.simulation import Simulation\n"

    assert _imported_modules(violation) & FORBIDDEN_MODULES == {
        "eudis_swarm.simulation"
    }
    assert _imported_names(violation) & FORBIDDEN_NAMES == {"Simulation"}


def test_relative_imports_are_caught_too() -> None:
    violation = "from .mission import Mission\n"

    assert _imported_modules(violation) & FORBIDDEN_MODULES == {"mission"}
    assert _imported_names(violation) & FORBIDDEN_NAMES == {"Mission"}
