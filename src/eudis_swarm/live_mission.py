"""Run one coordination mission and write a step-by-step evidence log.

This is the multi-vehicle SITL harness's simulator side. It subclasses
:class:`~.simulation.Simulation` only to *observe* and to call the extra
per-boundary hooks a :class:`~.mavlink_backend.MavlinkBackend` needs; it never
changes what a UAV knows, decides or owns. With the default
:class:`~.vehicle_backend.PointMassBackend` it produces exactly the same
mission as a plain ``Simulation`` (asserted by ``tests/test_live_mission.py``)
plus a deterministic JSON-lines log, so the same configuration can be flown on
live telemetry and replayed on the analytical model and the two logs compared
record for record.

Nothing in ``eudis_swarm.simulation`` or ``eudis_swarm.sweep`` imports this
module, and it imports no optional dependency: the live vehicles are handed in
already connected, through the backend.

A log produced with a live backend is **not** a measured result. SITL is not
reproducible; such numbers belong in docs/sitl.md, never in
docs/measured_results.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, TextIO

from .agent import Position
from .communication import CommunicationUpdate
from .config import SimulationConfig
from .mavlink_backend import MavlinkBackend
from .simulation import Simulation, SimulationResult
from .vehicle_backend import VehicleBackend

#: Bumped whenever a record's shape changes, so the report can refuse a log it
#: would misread.
LOG_SCHEMA_VERSION = 2

Record = dict[str, Any]
RecordSink = Callable[[Record], None]


def _point(position: Position) -> list[float]:
    return [float(position[0]), float(position[1])]


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (frozenset, set)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def config_record(config: SimulationConfig) -> Record:
    """Every configuration field, rendered as plain JSON values."""

    rendered = _jsonable(config)
    assert isinstance(rendered, dict)
    return rendered


class InstrumentedSimulation(Simulation):
    """A ``Simulation`` that logs each mission boundary and drives live hooks.

    Three private ``Simulation`` methods are extended, each strictly after or
    strictly before the original behaviour and never instead of it:

    * ``_advance_agents`` -- after the normal movement pass, a live backend
      brings every integrated vehicle (moving or hovering) to the boundary's
      logical time, so each vehicle's clock stays anchored.
    * ``_update_communication_graph`` -- it runs immediately after the fault
      schedule is applied, so a live backend gets its one chance to put a
      newly failed vehicle into a safe hold before anything else happens.
    * ``_observe_ownership`` -- the out-of-band evaluator's hook, called at
      t=0 and at every mission boundary; one ``step`` record is written there.

    The step record reads per-receiver ownership through
    :meth:`TaskClaimStore.view`, which is read-only; it is the same privileged
    observer view the ownership evaluator and trace recorder already use, and
    it is never fed back.
    """

    def __init__(
        self,
        config: SimulationConfig,
        *,
        sink: RecordSink,
        run_label: str,
        vehicle_backend: VehicleBackend | None = None,
        vehicle_types: Mapping[int, str] | None = None,
        completion_tolerances: Mapping[int, float] | None = None,
    ) -> None:
        super().__init__(config, vehicle_backend=vehicle_backend)
        self._vehicle_types = dict(vehicle_types or {})
        self._completion_tolerances = dict(completion_tolerances or {})
        for agent_id, tolerance in self._completion_tolerances.items():
            if agent_id not in self.mission.agents:
                raise ValueError(f"completion tolerance for unknown agent {agent_id}")
            if not tolerance > 0.0:
                raise ValueError("completion tolerances must be positive")
        self._sink = sink
        self._run_label = run_label
        self._live = (
            vehicle_backend if isinstance(vehicle_backend, MavlinkBackend) else None
        )
        self._event_cursor = 0
        self._fail_stop_cursor = 0

    @property
    def live_backend(self) -> MavlinkBackend | None:
        return self._live

    def _advance_agents(self, elapsed: float, timestamp: float) -> None:
        super()._advance_agents(elapsed, timestamp)
        if self._live is not None:
            self._live.synchronize(self.mission.ordered_agents, timestamp)

    def _update_communication_graph(
        self,
        timestamp: float,
        positions: Mapping[int, Position] | None = None,
    ) -> CommunicationUpdate:
        if self._live is not None:
            self._live.apply_physical_faults(self.mission.ordered_agents, timestamp)
        return super()._update_communication_graph(timestamp, positions)

    def _complete_arrivals(self, timestamp: float) -> int:
        """The simulator's arrival rule, with an optional per-agent tolerance.

        A copter settles 1-3 m from a setpoint and a rover well under one, so
        one tolerance cannot fit a mixed swarm. Without overrides this defers
        to ``Simulation._complete_arrivals`` unchanged; with them, the only
        difference is which distance counts as "arrived". Both backends of a
        comparison run with the same overrides.
        """

        if not self._completion_tolerances:
            return super()._complete_arrivals(timestamp)
        completed = 0
        for agent in self.mission.ordered_agents:
            task_id = self._executable_task_id(agent, timestamp)
            if task_id is None:
                continue
            objective = self._task_objectives_by_id[task_id]
            tolerance = self._completion_tolerances.get(
                agent.agent_id, self.config.completion_tolerance
            )
            if agent.distance_to(objective.position) <= tolerance:
                self.mission.complete_task(agent.agent_id, task_id, timestamp)
                completed += 1
        return completed

    def _observe_ownership(self, timestamp: float) -> None:
        super()._observe_ownership(timestamp)
        self._sink(self._step_record(timestamp))

    def run(self) -> SimulationResult:
        self._sink(self._header_record())
        result = super().run()
        self._sink(self._result_record(result))
        return result

    def _header_record(self) -> Record:
        live = self._live
        return {
            "type": "header",
            "schema": LOG_SCHEMA_VERSION,
            "run": self._run_label,
            "backend": "point-mass" if live is None else "mavlink",
            "deterministic": live is None,
            "integrated_agent_ids": [] if live is None else list(live.agent_ids),
            "fail_stop_mode": None if live is None else live.fail_stop_mode,
            "fail_stop_modes": (
                {}
                if live is None
                else {
                    str(agent_id): live.fail_stop_mode_for(agent_id)
                    for agent_id in live.agent_ids
                }
            ),
            "vehicle_types": {
                str(agent_id): kind
                for agent_id, kind in sorted(self._vehicle_types.items())
            },
            "completion_tolerances": {
                str(agent_id): tolerance
                for agent_id, tolerance in sorted(self._completion_tolerances.items())
            },
            "task_ids": [objective.task_id for objective in self.task_objectives],
            "task_positions": {
                str(objective.task_id): _point(objective.position)
                for objective in self.task_objectives
            },
            "agent_ids": [agent.agent_id for agent in self.mission.ordered_agents],
            "config": config_record(self.config),
        }

    def _step_record(self, timestamp: float) -> Record:
        agents = [
            {
                "id": agent.agent_id,
                "position": _point(agent.position),
                "responsive": agent.responsive,
                "status": agent.status.value,
                "current_task": agent.current_task,
            }
            for agent in self.mission.ordered_agents
        ]
        ownership: dict[str, list[list[Any]]] = {}
        for receiver_id in sorted(self.task_claim_stores):
            store = self.task_claim_stores[receiver_id]
            rows: list[list[Any]] = []
            for objective in self.task_objectives:
                view = store.view(objective.task_id, timestamp)
                rows.append(
                    [objective.task_id, view.state.value, view.known_owner_agent_id]
                )
            ownership[str(receiver_id)] = rows

        new_events = self.mission.events[self._event_cursor :]
        self._event_cursor = len(self.mission.events)
        events: list[Record] = [
            {
                "kind": event.kind.value,
                "t": event.timestamp,
                "agent_id": event.agent_id,
                "task_id": event.task_id,
            }
            for event in new_events
        ]

        vehicles: list[Record] = []
        live = self._live
        if live is not None:
            for fail_stop in live.fail_stops[self._fail_stop_cursor :]:
                events.append(
                    {
                        "kind": "FAIL_STOP",
                        "t": fail_stop.logical_time,
                        "agent_id": fail_stop.agent_id,
                        "task_id": None,
                        "mode": fail_stop.mode,
                        "confirmed": fail_stop.confirmed,
                        "vehicle_time_s": fail_stop.vehicle_time_s,
                        "position": _point(fail_stop.position),
                    }
                )
            self._fail_stop_cursor = len(live.fail_stops)
            for sample in live.drain_samples():
                vehicles.append(
                    {
                        "id": sample.agent_id,
                        "held": False,
                        "commanded": sample.commanded,
                        "position": _point(sample.position),
                        "vehicle_time_s": sample.vehicle_time_s,
                        "target_vehicle_time_s": sample.target_vehicle_time_s,
                        "drift_s": sample.drift_s,
                        "waited_wall_s": sample.waited_wall_s,
                        "altitude_m": sample.altitude_m,
                    }
                )
            for agent in self.mission.ordered_agents:
                observed = live.observe_held(agent.agent_id)
                if observed is None:
                    continue
                vehicles.append(
                    {
                        "id": agent.agent_id,
                        "held": True,
                        "commanded": False,
                        "position": _point(observed[1]),
                        "vehicle_time_s": observed[0],
                        "target_vehicle_time_s": None,
                        "drift_s": None,
                        "waited_wall_s": None,
                        "altitude_m": live.altitude_of(agent.agent_id),
                    }
                )
        return {
            "type": "step",
            "run": self._run_label,
            "t": timestamp,
            "agents": agents,
            "vehicles": vehicles,
            "ownership": ownership,
            "events": events,
        }

    def _result_record(self, result: SimulationResult) -> Record:
        metrics = result.metrics
        return {
            "type": "result",
            "run": self._run_label,
            "mission_completed": metrics.mission_completed,
            "completed_task_count": metrics.completed_task_count,
            "total_task_count": metrics.total_task_count,
            "mission_time_s": metrics.simulation_duration,
            "concurrent_ownership_task_s": metrics.concurrent_ownership_duration,
            "failure_detection_latency_s": {
                str(agent_id): latency
                for agent_id, latency in sorted(
                    metrics.failure_detection_latencies.items()
                )
            },
            "task_reassignment_latency_s": {
                str(task_id): latency
                for task_id, latency in sorted(
                    metrics.task_reassignment_latencies.items()
                )
            },
        }


def run_logged(
    config: SimulationConfig,
    *,
    run_label: str,
    vehicle_backend: VehicleBackend | None = None,
    vehicle_types: Mapping[int, str] | None = None,
    completion_tolerances: Mapping[int, float] | None = None,
) -> tuple[SimulationResult, list[Record]]:
    """Run one instrumented mission and return its result and every record."""

    records: list[Record] = []
    simulation = InstrumentedSimulation(
        config,
        sink=records.append,
        run_label=run_label,
        vehicle_backend=vehicle_backend,
        vehicle_types=vehicle_types,
        completion_tolerances=completion_tolerances,
    )
    return simulation.run(), records


def write_jsonl(records: list[Record], stream: TextIO) -> None:
    for record in records:
        stream.write(json.dumps(record, sort_keys=True, allow_nan=False))
        stream.write("\n")


def read_jsonl(stream: TextIO) -> list[Record]:
    records: list[Record] = []
    for number, line in enumerate(stream, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"line {number}: not JSON ({error.msg})") from error
        if not isinstance(record, dict) or "type" not in record:
            raise ValueError(f"line {number}: not an evidence-log record")
        records.append(record)
    return records
