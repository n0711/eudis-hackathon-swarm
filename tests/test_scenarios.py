"""Named demonstration scenarios.

A demo that needs eight flags does not get run in front of anybody. These
presets exist so each story is one command, and the tests pin the property that
makes each story worth showing rather than merely that it exits zero.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation, _parser, main


def _config(name: str) -> SimulationConfig:
    return replace(SimulationConfig(), **SCENARIO_PRESETS[name])  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(SCENARIO_PRESETS))
def test_every_preset_names_real_configuration_fields(name: str) -> None:
    fields = set(SimulationConfig.__dataclass_fields__)

    assert set(SCENARIO_PRESETS[name]) <= fields


@pytest.mark.parametrize("name", sorted(SCENARIO_PRESETS))
def test_every_preset_completes_its_mission(name: str) -> None:
    metrics = Simulation(_config(name)).run().metrics

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == metrics.total_task_count


def test_the_failstop_preset_actually_loses_and_detects_an_aircraft() -> None:
    """The connected story: a UAV really dies and the quorum notices."""

    metrics = Simulation(_config("demo-failstop")).run().metrics

    assert metrics.physically_failed_count == 1
    assert metrics.failed_agent_count == 1
    assert metrics.undetected_failure_count == 0
    assert metrics.simulation_duration == 17.25


def test_the_partition_preset_splits_the_swarm_in_half_and_heals() -> None:
    """Two components of two UAVs each, reconnected before the mission ends.

    The jammer geometry was measured rather than guessed, so this asserts the
    split really is balanced -- an unbalanced split would demonstrate isolation
    rather than split-brain.
    """

    trace = Simulation(_config("demo-partition"), capture_trace=True).run().trace
    assert trace is not None

    balanced = [
        frame.timestamp
        for frame in trace.frames
        if len(frame.components) == 2
        and sorted(len(component) for component in frame.components) == [2, 2]
    ]

    assert balanced, "the swarm never split into two equal halves"
    assert balanced[0] == pytest.approx(3.75)
    assert balanced[-1] == pytest.approx(7.25)

    # and it heals: the final frame is one connected component
    assert len(trace.frames[-1].components) == 1


def test_the_partition_preset_produces_measurable_wasted_work() -> None:
    """Without contention there is nothing for the demo to show."""

    metrics = Simulation(_config("demo-partition")).run().metrics

    assert metrics.concurrent_ownership_duration > 0.0
    components = metrics.maximum_connected_component_count
    assert components is not None
    assert components > 1


def test_the_partition_preset_injects_no_physical_failure() -> None:
    """The story is degraded comms, not a crash; keep the two separable."""

    metrics = Simulation(_config("demo-partition")).run().metrics

    assert metrics.physically_failed_count == 0
    assert metrics.failed_agent_count == 0


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_the_scenario_flag_is_offered_for_every_preset() -> None:
    arguments = _parser().parse_args([])

    assert arguments.scenario is None
    for name in SCENARIO_PRESETS:
        assert _parser().parse_args(["--scenario", name]).scenario == name


def test_an_unknown_scenario_is_rejected() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["--scenario", "not-a-scenario"])


@pytest.mark.parametrize("name", sorted(SCENARIO_PRESETS))
def test_the_cli_runs_each_preset(name: str) -> None:
    assert main(["--scenario", name, "--log-level", "ERROR"]) == 0


def test_a_preset_overrides_the_matching_command_line_option() -> None:
    """Documented precedence: the preset fixes the scenario it names."""

    contradicted = replace(SimulationConfig(), communication_range=130.0)

    applied = replace(contradicted, **SCENARIO_PRESETS["demo-partition"])  # type: ignore[arg-type]

    assert contradicted.communication_range == 130.0
    assert applied.communication_range == 50.0
    assert applied.region_jammer is not None


# --------------------------------------------------------------------------
# the running counter the dashboard displays
# --------------------------------------------------------------------------


def test_the_trace_carries_the_running_wasted_work_counter() -> None:
    """The dashboard shows this climbing; it must be in the frames to show."""

    trace = Simulation(_config("demo-partition"), capture_trace=True).run().trace
    assert trace is not None

    running = [frame.metrics.concurrent_ownership_duration for frame in trace.frames]

    assert running == sorted(running), "a time integral must never decrease"
    assert running[0] == 0.0
    assert running[-1] > 0.0
