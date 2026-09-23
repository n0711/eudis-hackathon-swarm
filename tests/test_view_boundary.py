"""Any view may only show what an agent actually received.

The belief panels the Streamlit dashboard used to render existed to demonstrate
that each UAV acts on partial, delayed, sometimes wrong information. That
demonstration is worthless if a renderer quietly reaches for world truth to
fill a gap, so this was enforced structurally rather than left as an
intention: the old dashboard modules were statically checked to import the
trace and nothing else from the package.

The dashboard is gone (see CHANGELOG), and nothing in the tree currently
consumes the trace to render a view, so there is no module left to point the
guard at. The checker itself stays here, proven against literal violations
below, so the next consumer of ``trace.py`` -- a Gazebo-side visualization
panel or anything else -- can be dropped into ``VIEW_MODULES`` and the guard
is live again with no new machinery to write.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "eudis_swarm"

#: Populate with any module that renders or reconstructs belief from a
#: recorded trace, e.g. ("gazebo_view.py",), once one exists.
VIEW_MODULES: tuple[str, ...] = ()

#: Names that carry authoritative world state. A view that imports any of
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


def test_no_view_module_imports_world_truth() -> None:
    for module_name in VIEW_MODULES:
        source = (SOURCE_ROOT / module_name).read_text(encoding="utf-8")
        offending = _imported_modules(source) & FORBIDDEN_MODULES
        assert not offending, (
            f"{module_name} imports {sorted(offending)}, which carries "
            "authoritative world state. A view must reconstruct belief from "
            "the trace only."
        )


def test_no_view_module_names_authoritative_types() -> None:
    for module_name in VIEW_MODULES:
        source = (SOURCE_ROOT / module_name).read_text(encoding="utf-8")
        offending = _imported_names(source) & FORBIDDEN_NAMES
        assert not offending, (
            f"{module_name} imports {sorted(offending)}. Rendering must use "
            "the recorded per-agent trace fields, never a live authoritative "
            "object."
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
