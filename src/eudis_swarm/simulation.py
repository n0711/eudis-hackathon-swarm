"""Orchestrate deterministic world physics, network delivery, and local evidence.

World truth advances the simulation while agent decisions receive only self-state,
receiver-local time, and evidence the modeled network actually delivered.
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
from .autonomy import (
    ContactPolicy,
    CoordinationPolicy,
    LocalAutonomyKernel,
)
from .communication import CommunicationGraph, CommunicationUpdate, RegionJammer
from .config import SimulationConfig
from .failure_manager import FailureDeclaration, FailureManager
from .messaging import (
    DeliveryBatch,
    PeerStateTransport,
    ProtocolDeliveryBatch,
    TaskClaimTransport,
    TaskProtocolDeliveryBatch,
)
from .metrics import SimulationMetrics
from .mission import Mission
from .peer_state import PeerStateStore
from .simulation_events import (
    CommunicationEvent,
    CommunicationEventKind,
    PeerStateEvent,
    PeerStateEventKind,
)
from .task import Task, TaskOwnershipState
from .task_allocator import Allocation, CommunicationAwareTaskAllocator, TaskAllocator
from .task_claims import TaskClaimStore
from .task_utility import (
    LocalTaskUtility,
    ReceiverLocalTaskUtility,
    TaskObjective,
    TaskUtilityWeights,
)
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
    task_claim_stores: dict[int, TaskClaimStore] | None = None
    autonomy_kernels: dict[int, LocalAutonomyKernel] | None = None
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
            selected_agent_ids,
            config.communication_range,
            radio_model=(config.radio_model if config.link_model == "radio" else None),
            stochastic_links=config.stochastic_delivery,
            link_seed=config.random_seed,
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
        # ownership is replicated per UAV, so a partitioned owner keeps a claim
        # the rest of the swarm cannot see or revoke.
        self.task_claim_stores = {
            owner_agent_id: TaskClaimStore(
                owner_agent_id,
                selected_agent_ids,
                tuple(task.task_id for task in selected_tasks),
                config.claim_lease_duration,
            )
            for owner_agent_id in sorted(selected_agent_ids)
        }
        contact_degraded_after = max(
            config.heartbeat_interval,
            config.peer_state_stale_after,
        )
        self.autonomy_kernels = {
            owner_agent_id: LocalAutonomyKernel(
                owner_agent_id,
                (
                    peer_agent_id
                    for peer_agent_id in selected_agent_ids
                    if peer_agent_id != owner_agent_id
                ),
                (task.task_id for task in selected_tasks),
                contact_policy=ContactPolicy(
                    expected_interval=config.heartbeat_interval,
                    degraded_after=contact_degraded_after,
                    lost_after=(contact_degraded_after + config.peer_state_stale_after),
                ),
                coordination_policy=CoordinationPolicy(
                    degradation_grace=config.heartbeat_interval,
                    local_autonomy_grace=config.heartbeat_interval,
                    recovery_stable_for=config.heartbeat_interval,
                ),
            )
            for owner_agent_id in sorted(selected_agent_ids)
        }
        self.task_claim_transport = TaskClaimTransport(
            self.communication_graph, self.task_claim_stores
        )
        self.task_objectives = tuple(
            sorted(
                (
                    TaskObjective(task_id=task.task_id, position=task.position)
                    for task in selected_tasks
                ),
                key=lambda objective: objective.task_id,
            )
        )
        self._task_objectives_by_id = {
            objective.task_id: objective for objective in self.task_objectives
        }
        utility_weights = TaskUtilityWeights()
        if config.allocation_policy == "connectivity":
            # One locally observed degree step dominates any in-area travel
            # difference, preserving the existing connectivity-first option
            # without giving the scorer graph access.
            utility_weights = TaskUtilityWeights(
                distance=1.0,
                communication=(2.0 * math.hypot(config.area_width, config.area_height)),
            )
        self.task_utility = ReceiverLocalTaskUtility(utility_weights)
        self._pending_task_utilities: dict[tuple[int, int], LocalTaskUtility] = {}
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
            task_claim_stores=self.task_claim_stores,
            distributed_task_control=True,
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
            self.task_claim_stores,
            self.autonomy_kernels,
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

    def _executable_task_id(self, agent: Agent, timestamp: float) -> int | None:
        """Resolve an execution intent through this UAV's own claim replica."""

        task_id = agent.current_task
        if task_id is None or not self.mission.task_is_executable(
            agent.agent_id,
            task_id,
            timestamp,
        ):
            return None
        return task_id

    def _advance_agents(self, elapsed: float, timestamp: float) -> None:
        for agent in self.mission.ordered_agents:
            task_id = self._executable_task_id(agent, timestamp)
            if task_id is None:
                continue
            objective = self._task_objectives_by_id[task_id]
            agent.move_toward(objective.position, elapsed)

    def _project_positions(
        self, elapsed: float, timestamp: float
    ) -> dict[int, Position]:
        """Project a graph-only snapshot without mutating physical agent state."""

        if elapsed < 0.0:
            raise ValueError("elapsed must be non-negative")
        positions: dict[int, Position] = {}
        for agent in self.mission.ordered_agents:
            task_id = self._executable_task_id(agent, timestamp)
            if task_id is None:
                positions[agent.agent_id] = agent.position
                continue
            target = self._task_objectives_by_id[task_id].position
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
            task_id = self._executable_task_id(agent, timestamp)
            if task_id is None:
                continue
            objective = self._task_objectives_by_id[task_id]
            if (
                agent.distance_to(objective.position)
                <= self.config.completion_tolerance
            ):
                self.mission.complete_task(agent.agent_id, task_id, timestamp)
                completed += 1
        return completed

    def _complete_initial_arrivals(self) -> None:
        """Resolve zero-time work without introducing a movement boundary."""

        self._exchange_claim_evidence(0.0)
        while self._complete_arrivals(0.0):
            self._exchange_claim_evidence(0.0)

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
            self.autonomy_kernels[observer_agent_id].advance_time(timestamp)
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
        for observer_agent_id, peer_agent_id in batch.delivered_observations:
            self.autonomy_kernels[observer_agent_id].receive_peer_evidence(
                peer_agent_id,
                timestamp,
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

    def _synchronize_local_autonomy(
        self,
        timestamp: float,
        participating_agent_ids: tuple[int, ...] | None = None,
    ) -> None:
        """Compose receiver-local stores without exposing graph or world truth."""

        participants = (
            self._participating_agent_ids()
            if participating_agent_ids is None
            else participating_agent_ids
        )
        for observer_agent_id in participants:
            kernel = self.autonomy_kernels[observer_agent_id]
            peer_store = self.peer_state_stores[observer_agent_id]
            for peer_agent_id in peer_store.peer_agent_ids:
                kernel.synchronize_peer_status(
                    peer_agent_id,
                    peer_store.status_for(peer_agent_id),
                    timestamp,
                )
            task_store = self.task_claim_stores[observer_agent_id]
            for task_id in task_store.task_ids:
                kernel.observe_task_view(
                    task_store.view(task_id, timestamp),
                    timestamp,
                )
        for observer_agent_id in participants:
            self.autonomy_kernels[observer_agent_id].evaluate_coordination(timestamp)

    def _record_protocol_delivery(
        self,
        timestamp: float,
        batch: ProtocolDeliveryBatch | TaskProtocolDeliveryBatch,
    ) -> None:
        """Record observer counters and expose only successful hops to receivers."""

        self.mission.metrics.record_protocol_message_batch(
            timestamp,
            attempted=batch.attempted,
            delivered=batch.delivered,
            undelivered=batch.undelivered,
            forwarded=batch.forwarded,
            duplicates_suppressed=batch.duplicates_suppressed,
            useful_first_deliveries=batch.useful_first_deliveries,
            duplicate_source_publications=batch.duplicate_source_publications,
            duplicate_route_suppressions=batch.duplicate_route_suppressions,
            inactive_endpoint_deferrals=batch.inactive_endpoint_deferrals,
        )
        for receipt in batch.receipts:
            if receipt.forwarder_agent_id == receipt.receiver_agent_id:
                continue
            self.autonomy_kernels[receipt.receiver_agent_id].receive_forwarded_evidence(
                receipt.forwarder_agent_id,
                timestamp,
            )

    def _local_communication_costs(self, agent_id: int) -> dict[int, float]:
        """Derive optional task costs from this receiver's delivered peer copies."""

        if self.config.allocation_policy != "connectivity":
            return {}
        heard_positions = tuple(
            observation.snapshot.position
            for observation in self.peer_state_stores[agent_id].heard_observations
        )
        maximum_peer_count = len(self.mission.agents) - 1
        costs: dict[int, float] = {}
        for objective in self.task_objectives:
            predicted_degree = sum(
                math.hypot(
                    objective.position[0] - peer_position[0],
                    objective.position[1] - peer_position[1],
                )
                <= self.config.communication_range
                for peer_position in heard_positions
            )
            costs[objective.task_id] = float(maximum_peer_count - predicted_degree)
        return costs

    def _rank_local_objectives(
        self,
        agent: Agent,
        objectives: Sequence[TaskObjective],
    ) -> tuple[LocalTaskUtility, ...]:
        return self.task_utility.rank(
            agent.agent_id,
            agent.position,
            objectives,
            communication_cost=self._local_communication_costs(agent.agent_id),
        )

    def _create_local_claim_intents(self, timestamp: float) -> int:
        """Batch independent claim choices from pre-mutation receiver-local views."""

        intents: list[LocalTaskUtility] = []
        for agent in self.mission.ordered_agents:
            if not agent.responsive or agent.current_task is not None:
                continue
            store = self.task_claim_stores[agent.agent_id]
            if any(
                store.view(task_id, timestamp).state is TaskOwnershipState.OWNED_BY_SELF
                for task_id in store.task_ids
            ):
                continue
            candidates = tuple(
                objective
                for objective in self.task_objectives
                if store.can_create_claim(objective.task_id, timestamp)
            )
            ranked = self._rank_local_objectives(agent, candidates)
            if ranked:
                intents.append(ranked[0])

        # Applying after every receiver has chosen prevents Python iteration order
        # from acting as a hidden central allocator.
        for utility in intents:
            store = self.task_claim_stores[utility.agent_id]
            store.create_claim(utility.task_id, timestamp)
            self._pending_task_utilities[(utility.agent_id, utility.task_id)] = utility
            self.mission.record_task_claim(
                utility.agent_id,
                utility.task_id,
                timestamp,
            )
        return len(intents)

    def _gossip_task_evidence(
        self,
        timestamp: float,
        participants: tuple[int, ...],
    ) -> None:
        claims = tuple(
            claim
            for agent_id in participants
            for claim in self.task_claim_stores[agent_id].claims_for_broadcast(
                timestamp
            )
        )
        releases = tuple(
            release
            for agent_id in participants
            for release in self.task_claim_stores[agent_id].releases_for_broadcast()
        )
        completions = tuple(
            completion
            for agent_id in participants
            for completion in self.task_claim_stores[
                agent_id
            ].completions_for_broadcast()
        )
        batches = (
            self.task_claim_transport.deliver_claims(
                claims,
                timestamp,
                receiving_agent_ids=participants,
            ),
            self.task_claim_transport.deliver_releases(
                releases,
                timestamp,
                receiving_agent_ids=participants,
            ),
            self.task_claim_transport.deliver_completions(
                completions,
                timestamp,
                receiving_agent_ids=participants,
            ),
        )
        for batch in batches:
            self._record_protocol_delivery(timestamp, batch)

    def _resolve_claims_and_stand_down(
        self,
        timestamp: float,
        participants: tuple[int, ...],
    ) -> int:
        local_conflict_losses: set[tuple[int, int]] = set()
        # Capture the receiver-local contest boundary before deterministic
        # reconciliation removes it at the same simulator timestamp.
        self._synchronize_local_autonomy(timestamp, participants)
        for agent_id in participants:
            store = self.task_claim_stores[agent_id]
            store.advance_time(timestamp)
            for decision in store.reconcile_all(timestamp):
                if decision.local_release is not None:
                    local_conflict_losses.add((agent_id, decision.task_id))

        stopped = 0
        for agent_id in participants:
            agent = self.mission.agents[agent_id]
            task_id = agent.current_task
            if task_id is None or self.task_claim_stores[agent_id].owns_task(
                task_id, timestamp
            ):
                continue
            if self.mission.stand_down_unowned_task(
                agent_id,
                task_id,
                timestamp,
                contested=(agent_id, task_id) in local_conflict_losses,
            ):
                stopped += 1
        return stopped

    def _allocation_from_utility(self, utility: LocalTaskUtility) -> Allocation:
        if self.config.allocation_policy == "distance":
            return Allocation(
                agent_id=utility.agent_id,
                task_id=utility.task_id,
                distance=utility.travel_cost,
            )
        maximum_peer_count = len(self.mission.agents) - 1
        predicted_degree = maximum_peer_count - round(utility.communication_cost)
        return Allocation(
            agent_id=utility.agent_id,
            task_id=utility.task_id,
            distance=utility.travel_cost,
            policy="connectivity",
            predicted_peer_degree=predicted_degree,
            predicted_isolation=predicted_degree == 0,
        )

    def _activate_local_owners(self, timestamp: float) -> int:
        activated = 0
        for agent in self.mission.ordered_agents:
            if not agent.responsive or agent.current_task is not None:
                continue
            store = self.task_claim_stores[agent.agent_id]
            owned_objectives = tuple(
                objective
                for objective in self.task_objectives
                if store.view(objective.task_id, timestamp).state
                is TaskOwnershipState.OWNED_BY_SELF
            )
            ranked = self._rank_local_objectives(agent, owned_objectives)
            if not ranked:
                continue
            selected = ranked[0]
            for surplus in ranked[1:]:
                store.release_claim(surplus.task_id, timestamp)
            if not self.mission.activate_claimed_task(
                agent.agent_id,
                selected.task_id,
                timestamp,
            ):
                continue
            utility = self._pending_task_utilities.pop(
                (agent.agent_id, selected.task_id),
                selected,
            )
            self.mission.metrics.record_allocation(
                self._allocation_from_utility(utility),
                timestamp,
            )
            activated += 1
        return activated

    def _exchange_claim_evidence(self, timestamp: float) -> None:
        """Run deterministic local claim/gossip/reconcile/execute rounds."""

        participants = self._participating_agent_ids()
        for agent_id in participants:
            self.task_claim_stores[agent_id].advance_time(timestamp)

        # Any pointer invalidated by expiry, a release, or completion is removed
        # before renewal and before it can authorize another physical action.
        self._resolve_claims_and_stand_down(timestamp, participants)
        for agent_id in participants:
            agent = self.mission.agents[agent_id]
            task_id = self._executable_task_id(agent, timestamp)
            if task_id is None:
                continue
            view = self.task_claim_stores[agent_id].view(task_id, timestamp)
            if (
                view.claim_age is not None
                and view.claim_age >= self.task_claim_stores[agent_id].freshness_timeout
            ):
                self.task_claim_stores[agent_id].renew_claim(task_id, timestamp)

        # One timestamp may need several claim waves: simultaneous candidates can
        # collide, converge, and let losers replan without moving in between.
        for _ in range(len(self.task_objectives) + len(participants) + 1):
            created = self._create_local_claim_intents(timestamp)
            self._gossip_task_evidence(timestamp, participants)
            stopped = self._resolve_claims_and_stand_down(timestamp, participants)
            activated = self._activate_local_owners(timestamp)
            if created == 0 and stopped == 0 and activated == 0:
                break
        else:
            raise RuntimeError("local task-claim rounds did not reach a fixed point")
        self._synchronize_local_autonomy(timestamp, participants)

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
        vote_batch = self.peer_state_transport.deliver_failure_votes(
            votes,
            manager,
            timestamp,
            receiving_agent_ids=participants,
        )
        self._record_protocol_delivery(timestamp, vote_batch)
        declarations = manager.detect_declarations(
            timestamp,
            participating_agent_ids=participants,
        )
        # world recovery consumes only new targets, while every locally
        # originated certificate remains eligible for network retransmission.
        outgoing_declarations = manager.declarations_for_broadcast(
            participating_agent_ids=participants,
        )
        declaration_batch = self.peer_state_transport.deliver_failure_declarations(
            outgoing_declarations,
            timestamp,
            receiving_agent_ids=participants,
        )
        self._record_protocol_delivery(timestamp, declaration_batch)
        self._apply_retractions(timestamp)
        return declarations

    def _apply_retractions(self, timestamp: float) -> None:
        """Withdraw declarations that a quorum of peers has heard disproved.

        A certificate is second-hand evidence.  Once enough of a UAV's peers
        have received a snapshot straight from it, the swarm has to admit the
        vehicle is alive -- otherwise a single outage would strike it off the
        mission permanently.
        """

        required = self.mission.failure_manager.required_votes
        for agent in self.mission.ordered_agents:
            if not agent.wrongly_declared:
                continue
            witnesses = sum(
                agent.agent_id in store.retracted_peer_ids
                for peer_agent_id, store in self.peer_state_stores.items()
                if peer_agent_id != agent.agent_id
            )
            if witnesses >= required:
                self.mission.retract_declaration(agent.agent_id, timestamp)

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

    def _active_blocked_links(
        self, positions: Mapping[int, Position], timestamp: float
    ) -> frozenset[tuple[int, int]]:
        """Combine static blocked links with any region jammer active right now."""

        blocked: set[tuple[int, int]] = set(self.config.blocked_links)
        jammer = self.config.region_jammer
        if jammer is not None and jammer.active_at(timestamp):
            for (
                source_agent_id,
                destination_agent_id,
            ) in self.communication_graph.pair_keys:
                if jammer.blocks_link(
                    positions[source_agent_id], positions[destination_agent_id]
                ):
                    blocked.add((source_agent_id, destination_agent_id))
        return frozenset(blocked)

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
            blocked_links=self._active_blocked_links(observed_positions, timestamp),
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
            if self.config.region_jammer is not None:
                # landing exactly on the jammer's edges keeps the partition it
                # creates aligned to its configured window.
                for jammer_edge in (
                    self.config.region_jammer.start_time,
                    self.config.region_jammer.end_time,
                ):
                    if jammer_edge > current_time + epsilon:
                        event_times.append(jammer_edge)
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
                self._advance_agents(
                    timestamp - last_physical_update_time,
                    timestamp,
                )
                last_physical_update_time = timestamp
                communication_positions: Mapping[int, Position] | None = None
            else:
                communication_positions = self._project_positions(
                    timestamp - last_physical_update_time,
                    timestamp,
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
                self._synchronize_local_autonomy(timestamp)
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
            self._exchange_claim_evidence(timestamp)
            completed_now = self._complete_arrivals(timestamp)
            if completed_now:
                # Completion evidence is seeded before the finish check, and
                # newly idle UAVs select another intent at the same timestamp.
                self._exchange_claim_evidence(timestamp)
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
            task_claim_stores=dict(self.task_claim_stores),
            autonomy_kernels=dict(self.autonomy_kernels),
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
        help="abstract maximum distance for an active communication link "
        "(used only by --link-model range)",
    )
    parser.add_argument(
        "--link-model",
        choices=("range", "radio"),
        default="range",
        help="communication link model: 'range' distance threshold (default) "
        "or 'radio' free-space line-of-sight BER model",
    )
    parser.add_argument(
        "--stochastic-delivery",
        action="store_true",
        help="with --link-model radio, sample each link's availability from its "
        "delivery probability using the run seed",
    )
    parser.add_argument(
        "--blocked-link",
        action="append",
        default=None,
        metavar="A:B",
        help="permanently block the undirected link between UAVs A and B (repeatable)",
    )
    parser.add_argument(
        "--jammer",
        type=str,
        default=None,
        metavar="X,Y,RADIUS,T_START,T_END",
        help="a circular jamming region: links whose midpoint falls inside the "
        "disc are down while T_START <= t < T_END",
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


def _parse_blocked_link(spec: str) -> tuple[int, int]:
    """Parse one ``A:B`` blocked-link CLI value into a UAV ID pair."""

    endpoints = spec.split(":")
    if len(endpoints) != 2:
        raise ValueError(f"--blocked-link expects 'A:B', got {spec!r}")
    try:
        left_agent_id, right_agent_id = (int(part) for part in endpoints)
    except ValueError:
        raise ValueError(
            f"--blocked-link endpoints must be integers, got {spec!r}"
        ) from None
    return (left_agent_id, right_agent_id)


def _parse_jammer(spec: str) -> RegionJammer:
    """Parse an ``X,Y,RADIUS,T_START,T_END`` jammer CLI value."""

    fields = spec.split(",")
    if len(fields) != 5:
        raise ValueError("--jammer expects 'X,Y,RADIUS,T_START,T_END'")
    try:
        center_x, center_y, radius, start_time, end_time = (
            float(field) for field in fields
        )
    except ValueError:
        raise ValueError("--jammer values must be numbers") from None
    return RegionJammer(center_x, center_y, radius, start_time, end_time)


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
            link_model=arguments.link_model,
            stochastic_delivery=arguments.stochastic_delivery,
            blocked_links=frozenset(
                _parse_blocked_link(spec) for spec in (arguments.blocked_link or ())
            ),
            region_jammer=(
                None if arguments.jammer is None else _parse_jammer(arguments.jammer)
            ),
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
