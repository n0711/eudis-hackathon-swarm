"""Fly one coordination mission on several ArduPilot SITL vehicles at once.

The command-line side of the multi-vehicle harness, behind
``scripts/sitl_swarm_mission.py``. It connects N vehicles started by
``scripts/sitl-swarm.sh``, arms and lifts them, runs one distributed-control
``Simulation`` mission with the chosen agents on live telemetry, schedules a
physical failure of one vehicle mid-mission (which puts the real aircraft into
a safe hold), and writes a JSON-lines evidence log. It then flies the
identical configuration on the deterministic point-mass backend and prints the
two side by side.

Nothing here imports pymavlink at module level: the default vehicle factory
imports ``eudis_swarm.sitl`` only when it is called, and tests hand in fakes.
Nothing in ``eudis_swarm.simulation`` or ``eudis_swarm.sweep`` imports this
module.

Every number this prints for a live run is **non-deterministic**. It belongs
in docs/sitl.md, never in docs/measured_results.md.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Protocol, TextIO

from .agent import Position
from .communication import RegionJammer
from .config import SimulationConfig
from .live_mission import Record, run_logged, write_jsonl
from .live_report import (
    DEFAULT_SEPARATION_THRESHOLD_M,
    check_test_card,
    format_checklist,
    format_comparison,
    format_vehicle_table,
    split_live_and_baseline,
    summarize,
)
from .mavlink_backend import SAFE_HOLD_MODES, MavlinkBackend, VehicleTelemetry
from .simulation import build_scenario

#: Where ``scripts/sitl-swarm.sh`` puts the simulator's (0, 0) by default.
#: Both sides must agree on it, so it is named once here and once there.
DEFAULT_ORIGIN = (35.1264, 33.4299)

#: Vehicle classes this harness can fly, and how each one is handled.
VEHICLE_TYPES = ("copter", "rover")

#: Safe hold per class: a copter brakes and hovers, a rover stops in place.
#: A copter's mode is the --fail-stop-mode choice; a rover always uses HOLD.
ROVER_HOLD_MODE = "HOLD"

#: Arrival tolerance per class, from measurement on ArduPilot SITL:
#: ArduCopter settles 1.4-2.0 m from a GUIDED setpoint (docs/sitl.md phase 3),
#: ArduRover 0.15-0.81 m (docs/data/sitl/rover_settle_2026-10-02.txt). The
#: rover value is that measured maximum with about 2x margin.
DEFAULT_COPTER_TOLERANCE_M = 2.0
DEFAULT_ROVER_TOLERANCE_M = 1.5

#: ArduPilot SITL serves instance ``i`` on TCP ``5760 + 10 * i``.
DEFAULT_BASE_PORT = 5760
PORT_STRIDE = 10


class FleetVehicle(VehicleTelemetry, Protocol):
    """What the mission needs from one vehicle beyond the backend's slice."""

    @property
    def altitude_m(self) -> float: ...

    def connect(self) -> None: ...

    def arm(self) -> None: ...

    def arm_and_takeoff(self, altitude_m: float) -> None: ...

    def set_mode(self, mode: str) -> bool: ...

    def close(self) -> None: ...


VehicleFactory = Callable[[str, tuple[float, float], int], FleetVehicle]


def _sitl_vehicle_factory(
    connection_string: str, origin: tuple[float, float], stream_rate_hz: int
) -> FleetVehicle:  # pragma: no cover - needs the sitl extra and a live autopilot
    from .sitl import SitlVehicle

    return SitlVehicle(
        connection_string,
        timeout=60.0,
        origin=origin,
        stream_rate_hz=stream_rate_hz,
    )


def _parse_origin(spec: str) -> tuple[float, float]:
    parts = spec.split(",")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("origin must be LAT,LON[,ALT,HEADING]")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"origin: {error}") from error


def _parse_ids(spec: str) -> tuple[int, ...]:
    try:
        ids = tuple(sorted({int(part) for part in spec.split(",") if part.strip()}))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"agent list: {error}") from error
    if not ids or ids[0] < 1:
        raise argparse.ArgumentTypeError("agent ids must be positive integers")
    return ids


def _parse_vehicle_types(spec: str) -> tuple[str, ...]:
    kinds = tuple(part.strip().lower() for part in spec.split(",") if part.strip())
    unknown = [kind for kind in kinds if kind not in VEHICLE_TYPES]
    if not kinds or unknown:
        raise argparse.ArgumentTypeError(
            f"vehicle types must be a comma-separated list of {VEHICLE_TYPES}"
        )
    return kinds


def _parse_jammer(spec: str) -> RegionJammer:
    try:
        x, y, radius, start, end = (float(part) for part in spec.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "jammer must be X,Y,RADIUS,START,END"
        ) from error
    return RegionJammer(x, y, radius, start, end)


def _parse_comm_fault(spec: str) -> tuple[int, float, float]:
    try:
        agent, start, end = spec.split(",")
        return int(agent), float(start), float(end)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "comm fault must be AGENT,START,END"
        ) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitl_swarm_mission.py",
        description=(
            "Run one distributed-control mission with N ArduPilot SITL vehicles "
            "on live telemetry, inject a physical failure with a real fail-stop, "
            "and compare against the deterministic point-mass backend. "
            "Live numbers are NON-deterministic: docs/sitl.md only."
        ),
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--connect",
        nargs="+",
        metavar="CONNECTION",
        help="one pymavlink connection string per agent, agent 1 first",
    )
    where.add_argument(
        "--host",
        default="127.0.0.1",
        help="SITL host; agent k is tcp:HOST:BASE_PORT+10*(k-1) (default 127.0.0.1)",
    )
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument(
        "--count", type=int, default=4, help="number of UAVs in the mission"
    )
    parser.add_argument(
        "--vehicles",
        type=_parse_vehicle_types,
        default=("copter",),
        help=(
            "vehicle type per agent, agent 1 first: copter or rover, e.g. "
            "copter,copter,copter,rover; one value applies to all (default copter). "
            "Must match the launcher's VEHICLES"
        ),
    )
    parser.add_argument(
        "--live-agents",
        type=_parse_ids,
        default=None,
        help="comma-separated agent ids on live telemetry (default: all)",
    )
    parser.add_argument(
        "--origin",
        type=_parse_origin,
        default=DEFAULT_ORIGIN,
        help="LAT,LON of the simulator's (0,0); must match sitl-swarm.sh ORIGIN",
    )
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--max-time", type=float, default=240.0)
    parser.add_argument(
        "--completion-tolerance",
        type=float,
        default=DEFAULT_COPTER_TOLERANCE_M,
        help=(
            "copter arrival tolerance in metres; a copter settles 1.4-2.0 m from "
            "a setpoint (default 2.0)"
        ),
    )
    parser.add_argument(
        "--rover-tolerance",
        type=float,
        default=DEFAULT_ROVER_TOLERANCE_M,
        help="rover arrival tolerance in metres; measured settle 0.15-0.81 m (default 1.5)",
    )
    parser.add_argument("--altitude", type=float, default=20.0)
    parser.add_argument(
        "--altitude-step",
        type=float,
        default=5.0,
        help=(
            "altitude layering: copter k (0-based, in agent order) cruises at "
            "--altitude + k * this, so no two copters share an altitude (default 5)"
        ),
    )
    parser.add_argument("--failure-agent", type=int, default=2)
    parser.add_argument("--failure-time", type=float, default=4.0)
    parser.add_argument(
        "--no-failure", action="store_true", help="schedule no physical failure"
    )
    parser.add_argument(
        "--fail-stop-mode",
        choices=[*SAFE_HOLD_MODES, "none"],
        default="BRAKE",
        help=(
            "safe hold sent once to a physically failed copter (default BRAKE); "
            "a rover always gets HOLD; none disables the fail-stop"
        ),
    )
    parser.add_argument(
        "--separation-threshold",
        type=float,
        default=DEFAULT_SEPARATION_THRESHOLD_M,
        help="3-D metres below which two live vehicles count as too close (default 5)",
    )
    parser.add_argument("--jammer", type=_parse_jammer, default=None)
    parser.add_argument("--comm-fault", type=_parse_comm_fault, default=None)
    parser.add_argument(
        "--start-tolerance",
        type=float,
        default=8.0,
        help="metres a vehicle may sit from its simulated start before aborting",
    )
    parser.add_argument("--step-timeout", type=float, default=30.0)
    parser.add_argument(
        "--stream-rate",
        type=int,
        default=10,
        help=(
            "telemetry Hz requested from each vehicle; one report interval "
            "bounds the lock-step drift (default 10)"
        ),
    )
    parser.add_argument(
        "--log", default="sitl_swarm_evidence.jsonl", help="evidence log path"
    )
    parser.add_argument(
        "--no-land",
        action="store_true",
        help="leave the vehicles hovering afterwards instead of LAND",
    )
    return parser


def mission_config(arguments: argparse.Namespace) -> SimulationConfig:
    """The one configuration both backends fly."""

    config = replace(
        SimulationConfig(),
        agent_count=arguments.count,
        task_count=arguments.tasks,
        random_seed=arguments.seed,
        task_control="distributed",
        max_simulation_time=arguments.max_time,
        completion_tolerance=arguments.completion_tolerance,
        failure_agent_id=arguments.failure_agent,
        failure_time=(
            arguments.max_time * 10.0
            if arguments.no_failure
            else arguments.failure_time
        ),
        region_jammer=arguments.jammer,
    )
    if arguments.comm_fault is not None:
        agent, start, end = arguments.comm_fault
        config = replace(
            config,
            comm_fault_agent_id=agent,
            comm_fault_start=start,
            comm_fault_end=end,
        )
    return config


def connection_strings(
    arguments: argparse.Namespace, live_agents: Sequence[int]
) -> dict[int, str]:
    if arguments.connect is not None:
        if len(arguments.connect) < max(live_agents):
            raise ValueError(
                f"--connect lists {len(arguments.connect)} connections but agent "
                f"{max(live_agents)} is to be live"
            )
        return {agent: arguments.connect[agent - 1] for agent in live_agents}
    return {
        agent: f"tcp:{arguments.host}:{arguments.base_port + PORT_STRIDE * (agent - 1)}"
        for agent in live_agents
    }


def vehicle_types(arguments: argparse.Namespace, count: int) -> dict[int, str]:
    kinds = arguments.vehicles
    if len(kinds) == 1:
        kinds = kinds * count
    if len(kinds) != count:
        raise ValueError(
            f"--vehicles lists {len(kinds)} types for a {count}-UAV mission"
        )
    return {index + 1: kind for index, kind in enumerate(kinds)}


def completion_tolerances(
    arguments: argparse.Namespace, kinds: dict[int, str]
) -> dict[int, float]:
    """Per-agent overrides: rovers only; copters use the configured tolerance."""

    if arguments.rover_tolerance <= 0.0:
        raise ValueError("--rover-tolerance must be positive")
    return {
        agent_id: arguments.rover_tolerance
        for agent_id, kind in kinds.items()
        if kind == "rover"
    }


def cruise_altitudes(
    arguments: argparse.Namespace, kinds: dict[int, str], live_agents: Sequence[int]
) -> dict[int, float]:
    """Altitude layering: each live copter gets its own band; rovers none."""

    copters = [agent_id for agent_id in live_agents if kinds[agent_id] == "copter"]
    return {
        agent_id: arguments.altitude + arguments.altitude_step * index
        for index, agent_id in enumerate(copters)
    }


def start_positions(config: SimulationConfig) -> dict[int, Position]:
    agents, _tasks = build_scenario(config)
    return {agent.agent_id: agent.position for agent in agents}


def _distance(a: Position, b: Position) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _hold_all(
    fleet: dict[int, FleetVehicle], kinds: dict[int, str], out: TextIO
) -> None:
    for agent_id in sorted(fleet):
        mode = ROVER_HOLD_MODE if kinds[agent_id] == "rover" else "BRAKE"
        try:
            confirmed = fleet[agent_id].hold(mode)
        except Exception as error:  # noqa: BLE001 - best effort on the abort path
            print(f"  UAV {agent_id}: {mode} failed: {error}", file=out)
        else:
            print(
                f"  UAV {agent_id}: {mode} {'confirmed' if confirmed else 'UNCONFIRMED'}",
                file=out,
            )


def run(
    arguments: argparse.Namespace,
    *,
    vehicle_factory: VehicleFactory = _sitl_vehicle_factory,
    out: TextIO = sys.stdout,
) -> int:
    config = mission_config(arguments)
    live_agents = (
        arguments.live_agents
        if arguments.live_agents is not None
        else tuple(range(1, config.agent_count + 1))
    )
    if max(live_agents) > config.agent_count:
        raise ValueError(
            f"live agent {max(live_agents)} does not exist in a "
            f"{config.agent_count}-UAV mission"
        )
    connections = connection_strings(arguments, live_agents)
    starts = start_positions(config)
    kinds = vehicle_types(arguments, config.agent_count)
    tolerances = completion_tolerances(arguments, kinds)
    altitudes = cruise_altitudes(arguments, kinds, live_agents)
    copter_mode = (
        None if arguments.fail_stop_mode == "none" else arguments.fail_stop_mode
    )

    fleet: dict[int, FleetVehicle] = {}
    try:
        for agent_id in live_agents:
            vehicle = vehicle_factory(
                connections[agent_id], arguments.origin, arguments.stream_rate
            )
            print(
                f"UAV {agent_id}: connecting to {connections[agent_id]} ...", file=out
            )
            vehicle.connect()
            fleet[agent_id] = vehicle
            offset = _distance(vehicle.position, starts[agent_id])
            if offset > arguments.start_tolerance:
                raise RuntimeError(
                    f"UAV {agent_id} is at {vehicle.position[0]:.1f},"
                    f"{vehicle.position[1]:.1f} in the shared frame but its "
                    f"simulated start is {starts[agent_id][0]:.1f},"
                    f"{starts[agent_id][1]:.1f} ({offset:.1f} m off). Start SITL "
                    "with scripts/sitl-swarm.sh and the same --origin/ORIGIN."
                )
        for agent_id in live_agents:
            if kinds[agent_id] == "rover":
                print(f"UGV {agent_id} (rover): arming in GUIDED ...", file=out)
                fleet[agent_id].arm()
                print(f"UGV {agent_id}: armed on the ground", file=out)
                continue
            altitude = altitudes[agent_id]
            print(f"UAV {agent_id}: arming, takeoff to {altitude:.0f} m ...", file=out)
            fleet[agent_id].arm_and_takeoff(altitude)
            print(
                f"UAV {agent_id}: airborne at {fleet[agent_id].altitude_m:.1f} m",
                file=out,
            )

        backend = MavlinkBackend(
            dict(fleet),
            step_timeout=arguments.step_timeout,
            fail_stop_mode=copter_mode,
            fail_stop_modes={
                agent_id: ROVER_HOLD_MODE
                for agent_id in fleet
                if kinds[agent_id] == "rover"
            },
        )
        print("starting the live mission (NON-deterministic)", file=out)
        try:
            _live_result, live_records = run_logged(
                config,
                run_label="sitl",
                vehicle_backend=backend,
                vehicle_types=kinds,
                completion_tolerances=tolerances,
            )
        except BaseException:
            print(
                "mission aborted: putting every vehicle into a safe hold "
                "(BRAKE for copters, HOLD for rovers)",
                file=out,
            )
            _hold_all(fleet, kinds, out)
            raise
        if not arguments.no_land:
            for agent_id in sorted(fleet):
                fleet[agent_id].set_mode(
                    ROVER_HOLD_MODE if kinds[agent_id] == "rover" else "LAND"
                )
    finally:
        for vehicle in fleet.values():
            vehicle.close()

    _pm_result, point_mass_records = run_logged(
        config,
        run_label="point-mass",
        vehicle_types=kinds,
        completion_tolerances=tolerances,
    )
    records: list[Record] = [*live_records, *point_mass_records]
    with open(arguments.log, "w", encoding="utf-8") as stream:
        write_jsonl(records, stream)

    summaries = summarize(
        records, separation_threshold_m=arguments.separation_threshold
    )
    print(file=out)
    print(format_comparison(summaries), file=out)
    for summary in summaries:
        if summary.vehicles:
            print(file=out)
            print(format_vehicle_table(summary), file=out)
    live, baseline = split_live_and_baseline(summaries)
    if live is not None:
        print(file=out)
        print(format_checklist("this run", check_test_card(live, baseline)), file=out)
    print(f"\nevidence log: {arguments.log}", file=out)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    vehicle_factory: VehicleFactory = _sitl_vehicle_factory,
    out: TextIO = sys.stdout,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.count < 1:
        parser.error("--count must be at least 1")
    if arguments.stream_rate < 1:
        parser.error("--stream-rate must be at least 1")
    try:
        return run(arguments, vehicle_factory=vehicle_factory, out=out)
    except (ValueError, RuntimeError, TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
