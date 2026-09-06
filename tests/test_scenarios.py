"""Named demonstration scenarios.

A demo that needs eight flags does not get run in front of anybody. These
presets exist so each story is one command, and the tests pin the property that
makes each story worth showing rather than merely that it exits zero.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation, _parser, main

#: Presets that deliberately fail. `baseline-quorum-floor` exists to show a
#: mission that cannot finish because recovery waits on a declaration that can
#: never arrive; asserting it completes would erase the point of it.
EXPECTED_TO_FAIL = frozenset({"baseline-quorum-floor"})

COMPLETING_PRESETS = sorted(set(SCENARIO_PRESETS) - EXPECTED_TO_FAIL)


def _config(name: str) -> SimulationConfig:
    return replace(SimulationConfig(), **SCENARIO_PRESETS[name])  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(SCENARIO_PRESETS))
def test_every_preset_names_real_configuration_fields(name: str) -> None:
    fields = set(SimulationConfig.__dataclass_fields__)

    assert set(SCENARIO_PRESETS[name]) <= fields


@pytest.mark.parametrize("name", COMPLETING_PRESETS)
def test_every_preset_completes_its_mission(name: str) -> None:
    metrics = Simulation(_config(name)).run().metrics

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == metrics.total_task_count


@pytest.mark.parametrize("name", sorted(EXPECTED_TO_FAIL))
def test_a_deliberately_failing_preset_really_does_fail(name: str) -> None:
    """If this ever passes, the demonstration has silently stopped working."""

    metrics = Simulation(_config(name)).run().metrics

    assert metrics.mission_completed is False
    assert metrics.completed_task_count < metrics.total_task_count


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


@pytest.mark.parametrize("name", COMPLETING_PRESETS)
def test_the_cli_runs_each_preset(name: str) -> None:
    assert main(["--scenario", name, "--log-level", "ERROR"]) == 0


@pytest.mark.parametrize("name", sorted(EXPECTED_TO_FAIL))
def test_the_cli_reports_a_failed_mission_as_exit_one(name: str) -> None:
    """The exit code has to carry the outcome, not just the log."""

    assert main(["--scenario", name, "--log-level", "ERROR"]) == 1


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


# --------------------------------------------------------------------------
# centralized baseline
# --------------------------------------------------------------------------


def test_the_baseline_preset_runs_the_centralized_coordinator() -> None:
    assert SCENARIO_PRESETS["baseline"]["task_control"] == "centralized"

    metrics = Simulation(_config("baseline")).run().metrics

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20


def test_the_two_control_modes_fail_in_opposite_ways() -> None:
    """The comparison the split-screen view exists to show.

    A central coordinator serialises every assignment, so two UAVs never both
    believe they own a task -- but it hands work to UAVs it cannot reach, and
    that work sits stranded. Replicated ownership strands nothing, and pays for
    it with occasional double-booking during a partition. Neither is simply
    better, which is the point.
    """

    centralized = Simulation(_config("baseline")).run().metrics
    distributed = Simulation(_config("demo-partition")).run().metrics

    # both finish the mission
    assert centralized.completed_task_count == distributed.completed_task_count == 20

    # the central coordinator never double-books...
    assert centralized.concurrent_ownership_duration == 0.0
    # ...but strands work it cannot deliver
    assert centralized.dropped_ownership_time > 0.0

    # replicated ownership is the mirror image
    assert distributed.concurrent_ownership_duration > 0.0
    assert distributed.dropped_ownership_time == 0.0


def test_the_control_mode_is_configurable_and_validated() -> None:
    assert _parser().parse_args([]).task_control == "distributed"
    assert (
        _parser().parse_args(["--task-control", "centralized"]).task_control
        == "centralized"
    )
    with pytest.raises(SystemExit):
        _parser().parse_args(["--task-control", "anarchy"])
    with pytest.raises(ValueError, match="task_control"):
        replace(SimulationConfig(), task_control="anarchy")


def test_the_amendment_preset_diverges_intent_and_then_converges() -> None:
    """One command demonstrating why operator intent carries a version."""

    result = Simulation(_config("demo-amendment"), capture_trace=True).run()
    trace = result.trace
    assert trace is not None

    diverged = [
        frame
        for frame in trace.frames
        if len({agent.contract_version for agent in frame.agents}) > 1
    ]

    assert diverged, "the preset never split operator intent"
    versions = {agent.agent_id: agent.contract_version for agent in diverged[0].agents}
    assert versions[1] == 2, "the briefed UAV holds the amendment"
    assert {versions[peer] for peer in (2, 3, 4)} == {1}, "the rest do not yet"

    assert result.autonomy_kernels is not None
    assert {kernel.contract_version for kernel in result.autonomy_kernels.values()} == {
        2
    }
    assert result.metrics.mission_completed is True
    assert result.metrics.completed_task_count == 20


def test_the_quorum_floor_presets_are_the_finding_in_two_commands() -> None:
    """The sharpest result in the project, reduced to a pair of runs.

    Two aircraft, one of which really stops. The quorum threshold means one
    survivor is one witness, so neither mode can declare the loss. What differs
    is whether recovery needed the declaration.
    """

    recovers = Simulation(_config("demo-quorum-floor")).run().metrics
    strands = Simulation(_config("baseline-quorum-floor")).run().metrics

    # both are equally blind
    for metrics in (recovers, strands):
        assert metrics.physically_failed_count == 1
        assert metrics.failed_agent_count == 0
        assert metrics.undetected_failure_count == 1

    # only one of them finishes the work
    assert recovers.mission_completed is True
    assert recovers.completed_task_count == 3

    assert strands.mission_completed is False
    assert strands.completed_task_count == 2


def test_the_two_quorum_floor_presets_differ_only_by_control_mode() -> None:
    """A comparison is only fair if nothing else moved."""

    distributed = dict(SCENARIO_PRESETS["demo-quorum-floor"])
    centralized = dict(SCENARIO_PRESETS["baseline-quorum-floor"])

    assert centralized.pop("task_control") == "centralized"
    assert "task_control" not in distributed
    assert distributed == centralized


# --------------------------------------------------------------------------
# the live-pitch script
# --------------------------------------------------------------------------

PITCH_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pitch-demo.ps1"


def _pitch_script_source() -> str:
    assert PITCH_SCRIPT.is_file(), f"missing pitch script at {PITCH_SCRIPT}"
    return PITCH_SCRIPT.read_text(encoding="utf-8")


def test_the_pitch_script_only_names_scenarios_that_exist() -> None:
    """A renamed preset must break the build, not the demo."""

    source = _pitch_script_source()
    named = set(re.findall(r"Scenario\s*=\s*'([a-z0-9-]+)'", source))
    assert named, "the pitch script names no scenarios"
    assert named <= set(SCENARIO_PRESETS), sorted(named - set(SCENARIO_PRESETS))
    assert {"demo-quorum-floor", "baseline-quorum-floor"} <= named


def test_the_pitch_script_greps_for_lines_the_summary_actually_emits() -> None:
    """The script filters output by prefix, and a silent filter looks like success.

    If a summary label is reworded, the script would quietly print nothing on
    stage rather than failing, so the labels are pinned to the real summary here.
    """

    source = _pitch_script_source()
    block = source.split("$keyLines = @(", 1)[1].split(")", 1)[0]
    key_lines = re.findall(r"'([^']+)'", block)
    assert len(key_lines) >= 3

    summary = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["demo-quorum-floor"]),  # type: ignore[arg-type]
        )
        .run()
        .metrics.format_summary()
    )

    for label in key_lines:
        assert label in summary, f"pitch script greps for absent label: {label!r}"
