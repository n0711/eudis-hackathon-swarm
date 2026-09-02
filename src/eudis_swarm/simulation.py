"""Orchestrate deterministic world physics, network delivery, and local evidence.

World truth advances the simulation while agent decisions receive only self-state
and messages or link evidence that the modeled network actually delivered.
"""

from __future__ import annotations

import argparse
import logging
import math
import random
from dataclasses import dataclass
from typing import Mapping, Sequence

from . import __version__
from .agent import Agent, AgentStatus, Heartbeat, Position
from .communication import CommunicationGraph, CommunicationUpdate
from .config import SimulationConfig
from .failure_manager import FailureDeclaration, FailureManager
from .messaging import DeliveryBatch, PeerStateTransport
from .metrics import SimulationMetrics
from .mission import Mission
from .peer_state import PeerStateStore
from .simulation_events import (
    CommunicationEvent,
    CommunicationEventKind,
    PeerStateEvent,
    PeerStateEventKind,
)
from .task import Task
from .task_allocator import CommunicationAwareTaskAllocator, TaskAllocator
from .trace import SimulationTrace, TraceRecorder
from .validation import validate_timestamp

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    mission: Mission
    metrics: SimulationMetrics
    position_history: dict[int, tuple[tuple[float, Position], ...]]
    communication_graph: CommunicationGraph | None = None
    communication_events: tuple[CommunicationEvent, ...] = ()
    peer_state_stores: dict[int, PeerStateStore] | None = None
    peer_state_events: tuple[PeerStateEvent, ...] = ()
    trace: SimulationTrace | None = None


def _initial_positions(config: SimulationConfig) -> list[Position]:
    margin = min(config.area_width, config.area_height) * 0.05
    corners = [
        (margin, margin),
        (config.area_width - margin, margin),
        (margin, config.area_height - margin),
        (config.area_width - margin, config.area_height - margin),
    ]
    if config.agent_count <= len(corners):
        return corners[: config.agent_count]

    positions = list(corners)
    center = (config.area_width / 2.0, config.area_height / 2.0)
    radius = min(config.area_width, config.area_height) * 0.35
    for index in range(config.agent_count - len(corners)):
        angle = 2.0 * math.pi * index / (config.agent_count - len(corners))
        positions.append(
            (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))
        )
    return positions


def build_scenario(config: SimulationConfig) -> tuple[list[Agent], list[Task]]:
    """Create reproducible UAV and task positions from the configured seed."""

    agents = [
        Agent(agent_id=index + 1, position=position, speed=config.agent_speed)
        for index, position in enumerate(_initial_positions(config))
    ]
    randomizer = random.Random(config.random_seed)
    x_margin = config.area_width * 0.1
    y_margin = config.area_height * 0.1
    tasks = [
        Task(
            task_id=index + 1,
            position=(
                randomizer.uniform(x_margin, config.area_width - x_margin),
                randomizer.uniform(y_margin, config.area_height - y_margin),
            ),
        )
        for index in range(config.task_count)
    ]
    return agents, tasks


class Simulation:
    """Own the logical clock, movement, faults, topology, and peer delivery."""

    def __init__(
        self,
        config: SimulationConfig,
        *,
        agents: Sequence[Agent] | None = None,
        tasks: Sequence[Task] | None = None,
        capture_trace: bool = False,
    ) -> None:
        self.config = config
        scenario_agents, scenario_tasks = build_scenario(config)
        selected_agents = list(agents) if agents is not None else scenario_agents
        selected_tasks = list(tasks) if tasks is not None else scenario_tasks
        if len(selected_agents) != config.agent_count:
            raise ValueError("provided agents must match config.agent_count")
        if len(selected_tasks) != config.task_count:
            raise ValueError("provided tasks must match config.task_count")
        selected_agent_ids = {agent.agent_id for agent in selected_agents}
        if config.failure_agent_id not in selected_agent_ids:
            raise ValueError("failure_agent_id is not present in provided agents")
        if (
            config.comm_fault_agent_id is not None
            and config.comm_fault_agent_id not in selected_agent_ids
        ):
            raise ValueError("comm_fault_agent_id is not present in provided agents")

        metrics = SimulationMetrics(
            total_task_count=len(selected_tasks),
            agents_started=len(selected_agents),
            allocation_policy=config.allocation_policy,
        )
        self.communication_graph = CommunicationGraph(
            selected_agent_ids, config.communication_range
        )
        self.peer_state_stores = {
            owner_agent_id: PeerStateStore(
                owner_agent_id,
                (
                    peer_agent_id
                    for peer_agent_id in selected_agent_ids
                    if peer_agent_id != owner_agent_id
                ),
                config.peer_state_stale_after,
            )
            for owner_agent_id in sorted(selected_agent_ids)
        }
        allocator = (
            TaskAllocator()
            if config.allocation_policy == "distance"
            else CommunicationAwareTaskAllocator(
                self.peer_state_stores,
                config.communication_range,
            )
        )
        self.mission = Mission(
            agents=selected_agents,
            tasks=selected_tasks,
            allocator=allocator,
            failure_manager=FailureManager(
                config.failure_timeout,
                self.peer_state_stores,
            ),
            metrics=metrics,
        )
        self._history: dict[int, list[tuple[float, Position]]] = {
            agent.agent_id: [(0.0, agent.position)] for agent in selected_agents
        }
        self._last_position_record_time = 0.0
        self.communication_events: list[CommunicationEvent] = []
        self.peer_state_transport = PeerStateTransport(
            self.communication_graph, self.peer_state_stores
        )
        self.peer_state_events: list[PeerStateEvent] = []
        self._blocked_communication_agents: set[int] = set()
        self._last_communication_update: float | None = None
        self._last_communication_event: float | None = None
        self._has_run = False
        self._trace_recorder = TraceRecorder(config) if capture_trace else None

    def _capture_trace_frame(
        self,
        timestamp: float,
        positions: Mapping[int, Position] | None = None,
    ) -> None:
        if self._trace_recorder is None:
            return
        self._trace_recorder.capture(
            self.mission,
            self.communication_graph,
            self.peer_state_stores,
            self.communication_events,
            self.peer_state_events,
            timestamp,
            positions,
        )

    def _record_positions(
        self,
        timestamp: float,
        positions: Mapping[int, Position] | None = None,
    ) -> None:
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_position_record_time,
            name="position-history timestamp",
        )
        for agent in self.mission.ordered_agents:
            position = (
                agent.position if positions is None else positions[agent.agent_id]
            )
            self._history[agent.agent_id].append((timestamp, position))
        self._last_position_record_time = timestamp

    def _advance_agents(self, elapsed: float) -> None:
        for agent in self.mission.ordered_agents:
            if (
                agent.status is not AgentStatus.ACTIVE
                or agent.current_task is None
                or not agent.responsive
            ):
                continue
            task = self.mission.tasks[agent.current_task]
            agent.move_toward(task.position, elapsed)

    def _project_positions(self, elapsed: float) -> dict[int, Position]:
        """Project a graph-only snapshot without mutating physical agent state."""

        if elapsed < 0.0:
            raise ValueError("elapsed must be non-negative")
        positions: dict[int, Position] = {}
        for agent in self.mission.ordered_agents:
            if (
                agent.status is not AgentStatus.ACTIVE
                or agent.current_task is None
                or not agent.responsive
            ):
                positions[agent.agent_id] = agent.position
                continue
            target = self.mission.tasks[agent.current_task].position
            distance = agent.distance_to(target)
            if distance == 0.0:
                positions[agent.agent_id] = agent.position
                continue
            travel = min(agent.speed * elapsed, distance)
            ratio = travel / distance
            positions[agent.agent_id] = (
                agent.position[0] + (target[0] - agent.position[0]) * ratio,
                agent.position[1] + (target[1] - agent.position[1]) * ratio,
            )
        return positions

    def _complete_arrivals(self, timestamp: float) -> int:
        completed = 0
        for agent in self.mission.ordered_agents:
            if (
                agent.status is not AgentStatus.ACTIVE
                or agent.current_task is None
                or not agent.responsive
            ):
                continue
            task = self.mission.tasks[agent.current_task]
            if agent.distance_to(task.position) <= self.config.completion_tolerance:
                self.mission.complete_task(agent.agent_id, task.task_id, timestamp)
                completed += 1
        return completed

    def _complete_initial_arrivals(self) -> None:
        """Resolve zero-time work without introducing a movement boundary."""

        while self._complete_arrivals(0.0):
            self.mission.allocate_tasks(0.0)

    def _communication_event(
        self,
        kind: CommunicationEventKind,
        timestamp: float,
        *,
        agent_id: int | None = None,
        peer_agent_id: int | None = None,
        component_count: int | None = None,
    ) -> None:
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_communication_event,
            name="communication event timestamp",
        )
        self._last_communication_event = timestamp
        self.communication_events.append(
            CommunicationEvent(
                kind=kind,
                timestamp=timestamp,
                agent_id=agent_id,
                peer_agent_id=peer_agent_id,
                component_count=component_count,
            )
        )

    def _peer_state_event(
        self,
        kind: PeerStateEventKind,
        timestamp: float,
        observer_agent_id: int,
        peer_agent_id: int,
    ) -> None:
        self.peer_state_events.append(
            PeerStateEvent(
                kind=kind,
                timestamp=timestamp,
                observer_agent_id=observer_agent_id,
                peer_agent_id=peer_agent_id,
            )
        )

    def _stale_observation_count(self) -> int:
        return sum(
            len(store.stale_peer_ids) for store in self.peer_state_stores.values()
        )

    def _advance_peer_freshness(self, timestamp: float) -> None:
        stale_transitions = 0
        participating_agent_ids = frozenset(self._participating_agent_ids())
        for observer_agent_id, store in sorted(self.peer_state_stores.items()):
            # failed software cannot advance its private clock or local beliefs.
            if observer_agent_id not in participating_agent_ids:
                continue
            for peer_agent_id in store.advance_time(timestamp):
                stale_transitions += 1
                self._peer_state_event(
                    PeerStateEventKind.STALE,
                    timestamp,
                    observer_agent_id,
                    peer_agent_id,
                )
                LOGGER.info(
                    "[PEER] UAV %d state for UAV %d became STALE at t=%.2fs",
                    observer_agent_id,
                    peer_agent_id,
                    timestamp,
                )
        if stale_transitions:
            self.mission.metrics.record_peer_state_transitions(
                timestamp,
                stale_transitions=stale_transitions,
                simultaneous_stale=self._stale_observation_count(),
            )

    def _deliver_peer_state(
        self, snapshots: tuple[Heartbeat, ...], timestamp: float
    ) -> DeliveryBatch:
        batch = self.peer_state_transport.deliver(
            snapshots,
            timestamp,
            receiving_agent_ids=self._participating_agent_ids(),
        )
        self.mission.metrics.record_peer_message_batch(
            timestamp,
            attempted=batch.attempted,
            delivered=batch.delivered,
            undelivered=batch.undelivered,
        )
        for observer_agent_id, peer_agent_id in batch.refreshed_observations:
            self._peer_state_event(
                PeerStateEventKind.REFRESHED,
                timestamp,
                observer_agent_id,
                peer_agent_id,
            )
            LOGGER.info(
                "[PEER] UAV %d state for UAV %d refreshed at t=%.2fs",
                observer_agent_id,
                peer_agent_id,
                timestamp,
            )
        if batch.refreshed_observations:
            self.mission.metrics.record_peer_state_transitions(
                timestamp,
                refresh_transitions=len(batch.refreshed_observations),
                simultaneous_stale=self._stale_observation_count(),
            )
        LOGGER.debug(
            "[PEER] State batch at t=%.2fs: %d/%d delivered",
            timestamp,
            batch.delivered,
            batch.attempted,
        )
        return batch

    def _participating_agent_ids(self) -> tuple[int, ...]:
        """Return UAVs whose local software can execute at this world instant."""

        return tuple(
            agent.agent_id for agent in self.mission.ordered_agents if agent.responsive
        )

    def _exchange_failure_evidence(
        self, timestamp: float
    ) -> tuple[FailureDeclaration, ...]:
        """Route local suspicion and declaration messages through active links."""

        participants = self._participating_agent_ids()
        manager = self.mission.failure_manager
        votes = manager.propose_votes(
            timestamp,
            participating_agent_ids=participants,
        )
        self.peer_state_transport.deliver_failure_votes(
            votes,
            manager,
            timestamp,
            receiving_agent_ids=participants,
        )
        declarations = manager.detect_declarations(
            timestamp,
            participating_agent_ids=participants,
        )
        # world recovery consumes only new targets, while every locally
        # originated certificate remains eligible for network retransmission.
        outgoing_declarations = manager.declarations_for_broadcast(
            participating_agent_ids=participants,
        )
        self.peer_state_transport.deliver_failure_declarations(
            outgoing_declarations,
            timestamp,
            receiving_agent_ids=participants,
        )
        return declarations

    def _start_communication_fault(self, timestamp: float) -> None:
        agent_id = self.config.comm_fault_agent_id
        if agent_id is None:
            return
        self._blocked_communication_agents.add(agent_id)
        self._communication_event(
            CommunicationEventKind.FAULT_STARTED, timestamp, agent_id=agent_id
        )
        LOGGER.info(
            "[COMM-FAULT] Blocking communications for UAV %d at t=%.2fs",
            agent_id,
            timestamp,
        )

    def _end_communication_fault(self, timestamp: float) -> None:
        agent_id = self.config.comm_fault_agent_id
        if agent_id is None:
            return
        self._blocked_communication_agents.discard(agent_id)
        self._communication_event(
            CommunicationEventKind.FAULT_ENDED, timestamp, agent_id=agent_id
        )
        LOGGER.info(
            "[COMM-FAULT] Restoring communications for UAV %d at t=%.2fs",
            agent_id,
            timestamp,
        )

    def _update_communication_graph(
        self,
        timestamp: float,
        positions: Mapping[int, Position] | None = None,
    ) -> CommunicationUpdate:
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_communication_update,
            name="communication update timestamp",
        )
        observed_positions = (
            positions
            if positions is not None
            else {
                agent_id: agent.position
                for agent_id, agent in self.mission.agents.items()
            }
        )
        update = self.communication_graph.update(
            observed_positions,
            blocked_agent_ids=self._blocked_communication_agents,
        )
        self.peer_state_transport.synchronize_link_evidence(
            timestamp,
            observing_agent_ids=self._participating_agent_ids(),
        )
        self._last_communication_update = timestamp
        observed_unreachable_ids = (
            frozenset()
            if update.is_initial
            else frozenset(update.newly_isolated_agent_ids)
        )
        healthy_unreachable_ids = tuple(
            agent_id
            for agent_id in sorted(observed_unreachable_ids)
            if self.mission.agents[agent_id].responsive
            and self.mission.agents[agent_id].status is not AgentStatus.FAILED
        )
        self.mission.metrics.record_communication_update(
            timestamp,
            self.communication_graph,
            update,
            healthy_unreachable_ids,
        )

        if update.is_initial:
            self._communication_event(
                CommunicationEventKind.NETWORK_INITIALIZED,
                timestamp,
                component_count=update.component_count,
            )
            if self.communication_graph.is_fully_connected:
                LOGGER.info(
                    "[NETWORK] Network initially connected with %d links",
                    self.communication_graph.link_count,
                )
            else:
                LOGGER.info(
                    "[NETWORK] Network initially partitioned into %d components",
                    update.component_count,
                )
            for agent_id in sorted(self.communication_graph.isolated_agent_ids):
                LOGGER.info("[NETWORK] UAV %d initially unreachable", agent_id)
            return update

        for link in update.lost_links:
            self._communication_event(
                CommunicationEventKind.LINK_LOST,
                timestamp,
                agent_id=link.source_agent_id,
                peer_agent_id=link.destination_agent_id,
            )
            LOGGER.info(
                "[LINK] UAV %d <-> UAV %d LOST",
                link.source_agent_id,
                link.destination_agent_id,
            )
        for link in update.restored_links:
            self._communication_event(
                CommunicationEventKind.LINK_RESTORED,
                timestamp,
                agent_id=link.source_agent_id,
                peer_agent_id=link.destination_agent_id,
            )
            LOGGER.info(
                "[LINK] UAV %d <-> UAV %d RESTORED",
                link.source_agent_id,
                link.destination_agent_id,
            )

        if update.network_partitioned:
            self._communication_event(
                CommunicationEventKind.NETWORK_PARTITIONED,
                timestamp,
                component_count=update.component_count,
            )
            LOGGER.info(
                "[NETWORK] Network partitioned into %d components",
                update.component_count,
            )
        elif update.network_reconnected:
            self._communication_event(
                CommunicationEventKind.NETWORK_RECONNECTED,
                timestamp,
                component_count=update.component_count,
            )
            LOGGER.info("[NETWORK] Network reconnected")

        for agent_id in update.newly_isolated_agent_ids:
            self._communication_event(
                CommunicationEventKind.AGENT_UNREACHABLE,
                timestamp,
                agent_id=agent_id,
            )
            LOGGER.info("[NETWORK] UAV %d became unreachable", agent_id)
            if agent_id in healthy_unreachable_ids:
                LOGGER.info("[AGENT] UAV %d remains physically healthy", agent_id)
        for agent_id in update.newly_reachable_agent_ids:
            self._communication_event(
                CommunicationEventKind.AGENT_REACHABLE,
                timestamp,
                agent_id=agent_id,
            )
            LOGGER.info("[NETWORK] UAV %d reachable again", agent_id)
        return update

    def run(self) -> SimulationResult:
        if self._has_run:
            raise RuntimeError("a Simulation instance can only run once")
        self._has_run = True
        initial_snapshots = self.mission.start(0.0)
        failure_injected = False
        communication_fault_started = self.config.comm_fault_agent_id is None
        communication_fault_ended = self.config.comm_fault_agent_id is None
        current_time = 0.0
        last_physical_update_time = 0.0
        motion_step = 1
        next_motion = self.config.time_step
        next_heartbeat = self.config.heartbeat_interval
        epsilon = 1e-12

        self._update_communication_graph(0.0)

        if self.config.failure_time <= epsilon:
            failure_injected = self.mission.inject_failure(
                self.config.failure_agent_id, 0.0
            )
        if not communication_fault_started and self.config.comm_fault_start <= epsilon:
            self._start_communication_fault(0.0)
            communication_fault_started = True
            self._update_communication_graph(0.0)

        self._advance_peer_freshness(0.0)
        self._deliver_peer_state(initial_snapshots, 0.0)
        self._complete_initial_arrivals()
        self.mission.assert_consistent()
        if self.mission.all_tasks_completed:
            self.mission.finish(0.0, True)
        self._capture_trace_frame(0.0)

        while (
            not self.mission.finished
            and current_time < self.config.max_simulation_time - epsilon
        ):
            event_times = [
                next_motion,
                next_heartbeat,
                self.config.max_simulation_time,
            ]
            if (
                not failure_injected
                and self.config.failure_time > current_time + epsilon
            ):
                event_times.append(self.config.failure_time)
            if (
                not communication_fault_started
                and self.config.comm_fault_start > current_time + epsilon
            ):
                event_times.append(self.config.comm_fault_start)
            if (
                not communication_fault_ended
                and self.config.comm_fault_end > current_time + epsilon
            ):
                event_times.append(self.config.comm_fault_end)
            timestamp = validate_timestamp(
                round(
                    min(
                        value for value in event_times if value > current_time + epsilon
                    ),
                    12,
                ),
                previous=current_time,
                name="simulation timestamp",
            )
            current_time = timestamp

            motion_due = timestamp + epsilon >= next_motion
            heartbeat_due = timestamp + epsilon >= next_heartbeat
            failure_due = (
                not failure_injected and timestamp + epsilon >= self.config.failure_time
            )
            maximum_time_due = timestamp + epsilon >= self.config.max_simulation_time
            mission_boundary = (
                motion_due or heartbeat_due or failure_due or maximum_time_due
            )
            if mission_boundary:
                self._advance_agents(timestamp - last_physical_update_time)
                last_physical_update_time = timestamp
                communication_positions: Mapping[int, Position] | None = None
            else:
                communication_positions = self._project_positions(
                    timestamp - last_physical_update_time
                )

            # a failure scheduled on a boundary wins over heartbeat emission and
            # task completion at that same timestamp.
            if failure_due:
                failure_injected = self.mission.inject_failure(
                    self.config.failure_agent_id, timestamp
                )

            if (
                not communication_fault_started
                and timestamp + epsilon >= self.config.comm_fault_start
            ):
                self._start_communication_fault(timestamp)
                communication_fault_started = True
            if (
                not communication_fault_ended
                and timestamp + epsilon >= self.config.comm_fault_end
            ):
                self._end_communication_fault(timestamp)
                communication_fault_ended = True

            self._update_communication_graph(timestamp, communication_positions)
            self._advance_peer_freshness(timestamp)

            # communication-only boundaries are observational and must not
            # introduce extra task completion, allocation, or failure checks.
            if not mission_boundary:
                self._record_positions(timestamp, communication_positions)
                self.mission.assert_consistent()
                self._capture_trace_frame(timestamp, communication_positions)
                continue

            # publication precedes protocol-backed recovery and new allocation.
            if heartbeat_due:
                snapshots = self.mission.exchange_heartbeats(timestamp)
                self._deliver_peer_state(snapshots, timestamp)
                while next_heartbeat <= timestamp + epsilon:
                    next_heartbeat += self.config.heartbeat_interval

            declarations = self._exchange_failure_evidence(timestamp)
            self.mission.detect_and_recover(timestamp, declarations)
            self._complete_arrivals(timestamp)
            self.mission.allocate_tasks(timestamp)
            self._record_positions(timestamp)
            self.mission.assert_consistent()

            if self.mission.all_tasks_completed:
                self.mission.finish(timestamp, True)
                self._capture_trace_frame(timestamp)
                break
            self._capture_trace_frame(timestamp)
            if motion_due:
                while next_motion <= timestamp + epsilon:
                    motion_step += 1
                    next_motion = round(motion_step * self.config.time_step, 12)
        if not self.mission.finished:
            self.mission.finish(current_time, False)
            self._capture_trace_frame(current_time)

        trace = (
            None
            if self._trace_recorder is None
            else self._trace_recorder.finish(self.mission)
        )

        result = SimulationResult(
            mission=self.mission,
            metrics=self.mission.metrics,
            position_history={
                agent_id: tuple(entries) for agent_id, entries in self._history.items()
            },
            communication_graph=self.communication_graph,
            communication_events=tuple(self.communication_events),
            peer_state_stores=dict(self.peer_state_stores),
            peer_state_events=tuple(self.peer_state_events),
            trace=trace,
        )
        LOGGER.info("\n%s", result.metrics.format_summary())
        return result


def run_simulation(
    config: SimulationConfig | None = None, *, capture_trace: bool = False
) -> SimulationResult:
    return Simulation(config or SimulationConfig(), capture_trace=capture_trace).run()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(message)s", force=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the EUDIS resilient swarm Prototype 0.3A simulation."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"eudis-swarm {__version__}",
        help="print the package version and exit",
    )
    parser.add_argument("--agents", type=int, default=4, help="number of UAV agents")
    parser.add_argument("--tasks", type=int, default=20, help="number of mission tasks")
    parser.add_argument("--seed", type=int, default=2026, help="random seed")
    parser.add_argument("--failure-agent", type=int, default=2, help="UAV ID to fail")
    parser.add_argument(
        "--failure-time", type=float, default=4.0, help="failure injection time"
    )
    parser.add_argument(
        "--failure-timeout", type=float, default=2.5, help="heartbeat timeout"
    )
    parser.add_argument(
        "--peer-state-stale-after",
        type=float,
        default=2.5,
        help="strict peer-observation freshness timeout",
    )
    parser.add_argument(
        "--allocation-policy",
        choices=("distance", "connectivity"),
        default="distance",
        help="task allocation policy (default: distance)",
    )
    parser.add_argument(
        "--communication-range",
        type=float,
        default=130.0,
        help="abstract maximum distance for an active communication link",
    )
    parser.add_argument(
        "--comm-fault-agent",
        type=int,
        default=None,
        help="UAV ID to communication-isolate (omit to disable)",
    )
    parser.add_argument(
        "--comm-fault-start",
        type=float,
        default=4.0,
        help="communication fault start time",
    )
    parser.add_argument(
        "--comm-fault-end",
        type=float,
        default=8.0,
        help="communication fault restoration time",
    )
    parser.add_argument(
        "--visualize",
        action="store_true",
        help="show an optional final matplotlib view",
    )
    parser.add_argument(
        "--record-trace",
        type=str,
        default=None,
        metavar="PATH",
        help="write a structured dashboard playback trace to PATH",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        config = SimulationConfig(
            agent_count=arguments.agents,
            task_count=arguments.tasks,
            random_seed=arguments.seed,
            failure_agent_id=arguments.failure_agent,
            failure_time=arguments.failure_time,
            failure_timeout=arguments.failure_timeout,
            peer_state_stale_after=arguments.peer_state_stale_after,
            allocation_policy=arguments.allocation_policy,
            communication_range=arguments.communication_range,
            comm_fault_agent_id=arguments.comm_fault_agent,
            comm_fault_start=arguments.comm_fault_start,
            comm_fault_end=arguments.comm_fault_end,
        )
    except ValueError as error:
        parser.error(str(error))

    configure_logging(arguments.log_level)
    result = (
        run_simulation(config)
        if arguments.record_trace is None
        else run_simulation(config, capture_trace=True)
    )
    if arguments.record_trace is not None:
        if result.trace is None:
            raise RuntimeError("trace capture was requested but no trace was produced")
        result.trace.write_json(arguments.record_trace)
        LOGGER.info("[TRACE] Wrote playback trace to %s", arguments.record_trace)
    if arguments.visualize:
        try:
            from .visualization import show_result

            show_result(result, config)
        except ImportError:
            LOGGER.error(
                "[VIS] matplotlib is not installed; use the 'visualization' extra"
            )
            return 2
    return 0 if result.metrics.mission_completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
