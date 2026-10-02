"""Run one UAV of the swarm as its own OS process, talking real UDP.

One process is one UAV. It owns exactly one
:class:`~eudis_swarm.peer_state.PeerStateStore`, one
:class:`~eudis_swarm.task_claims.TaskClaimStore`, one
:class:`~eudis_swarm.field_detector.LocalFailureDetector`, the immutable
:class:`~eudis_swarm.task_utility.TaskObjective` catalogue from the shared
mission file, and its own position. It never reads another node's state: the
only way anything about a peer reaches it is a datagram that actually arrived,
stamped with this process's clock on arrival.

**Not deterministic.** Like :class:`~eudis_swarm.sitl.SitlVehicle`, a field node
runs on a real clock over a real network, so :attr:`FieldNode.deterministic` is
``False`` and nothing it produces may stand in for a measured simulator result.
The deterministic simulator does not import this module, and a test enforces
that boundary.

**One loop iteration** (:meth:`FieldNode.step`), on the node's own monotonic
clock (seconds since the node started, which is also its *source* clock for
the timestamps it puts on its own evidence):

1. fail-stop if the fault injector says so; refresh the lab drop filter;
2. drain the socket: apply each heartbeat to the peer store (one-hop only,
   never forwarded); admit each piece of protocol evidence once per structural
   ID, apply it, and forward it on its first delivery;
3. age peer freshness; on the heartbeat cadence send a heartbeat, propose
   failure votes from local silence and declare only on a quorum;
4. one task-control round, the single-UAV image of the simulator's
   ``_exchange_claim_evidence``: advance the lease clock, reconcile visible
   contests (the local loser emits an exact-generation release), stand down
   from anything no longer ``owns_task()``, renew a held claim once it reaches
   ``freshness_timeout``, and when idle choose the best locally claimable
   objective and **create a claim before binding**; bind only after
   ``claim_settle`` and only while still owned;
5. move toward the bound objective only while ``owns_task()`` (point mass on
   real elapsed time, or a live vehicle); on arrival emit completion evidence;
6. on the rebroadcast cadence re-offer own live evidence and held relay
   evidence (the real-network form of store-and-forward retry);
7. log every belief change as one JSONL record.

Every record carries the node's local time, wall time (observer alignment only;
never read by any decision), the source time of the evidence involved where
there is one, the kind, the full per-task ownership view, and every peer's
status.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import IO, Any, Protocol

from . import __version__
from .agent import AgentStatus, Heartbeat, Position, advance_point_mass
from .failure_manager import FailureDeclaration, FailureVote
from .field_codec import FieldMessage
from .field_detector import LocalFailureDetector
from .field_faults import FaultInjector, parse_drop_window
from .field_mission import FieldMission, load_mission
from .field_transport import (
    Address,
    DatagramSocket,
    EvidenceRelay,
    ReceivedMessage,
    UdpTransport,
    open_udp_socket,
    origin_agent_id,
    parse_address,
)
from .mavlink_backend import VehicleTelemetry
from .peer_state import PeerStateStore
from .task import TaskOwnershipState
from .task_claims import (
    TaskClaim,
    TaskClaimRelease,
    TaskClaimStore,
    TaskCompletionEvidence,
)
from .task_utility import ReceiverLocalTaskUtility, TaskObjective

LOG_FORMAT = "eudis-field-log"
LOG_VERSION = 1

#: Rejections of each reason that are logged individually before only the
#: counter keeps going -- a flood of garbage must not become a flood of disk.
_REJECTION_LOG_LIMIT = 5


class FieldVehicle(Protocol):
    """What the node needs from whatever physically moves."""

    deterministic: bool

    @property
    def position(self) -> Position: ...

    def move_toward(self, target: Position, elapsed: float) -> Position: ...

    def hold(self) -> None: ...


class PointMassVehicle:
    """Constant-speed point mass advanced by real elapsed time."""

    deterministic = False

    def __init__(self, start: Position, speed: float) -> None:
        if speed <= 0.0:
            raise ValueError("speed must be positive")
        self._position = (float(start[0]), float(start[1]))
        self._speed = speed

    @property
    def position(self) -> Position:
        return self._position

    def move_toward(self, target: Position, elapsed: float) -> Position:
        self._position = advance_point_mass(
            self._position, target, self._speed, max(0.0, elapsed)
        )
        return self._position

    def hold(self) -> None:
        return None


class MavlinkFieldVehicle:
    """Drive a live vehicle (SITL or real autopilot) as this node's body.

    The vehicle's local frame is anchored where it powered up; the mission
    frame puts this agent at ``origin``. Positions are translated both ways.
    Telemetry is world truth about *this* aircraft only, which is the one
    thing a node may know first-hand.
    """

    deterministic = False

    def __init__(
        self,
        telemetry: VehicleTelemetry,
        origin: Position,
        *,
        command_interval: float = 1.0,
    ) -> None:
        self._telemetry = telemetry
        self._origin = origin
        self._command_interval = command_interval
        self._last_target: Position | None = None
        self._since_command = 0.0

    @property
    def position(self) -> Position:
        local = self._telemetry.position
        return (self._origin[0] + local[0], self._origin[1] + local[1])

    def move_toward(self, target: Position, elapsed: float) -> Position:
        self._since_command += max(0.0, elapsed)
        if target != self._last_target or self._since_command >= self._command_interval:
            self._telemetry.command_toward(
                (target[0] - self._origin[0], target[1] - self._origin[1])
            )
            self._last_target = target
            self._since_command = 0.0
        self._telemetry.observe(0.0)
        return self.position

    def hold(self) -> None:
        self._telemetry.observe(0.0)
        self._telemetry.command_toward(self._telemetry.position)
        self._last_target = None


def open_mavlink_vehicle(
    connection_string: str, origin: Position, takeoff_altitude: float | None
) -> MavlinkFieldVehicle:  # pragma: no cover - needs a live autopilot
    """Connect a SITL/real vehicle; pymavlink is imported only here."""

    from .sitl import SitlVehicle

    vehicle = SitlVehicle(connection_string)
    vehicle.connect()
    if takeoff_altitude is not None:
        vehicle.arm_and_takeoff(takeoff_altitude)
    return MavlinkFieldVehicle(vehicle, origin)


class JsonlLog:
    """Append one JSON object per line, flushed so a killed node keeps its log."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    @classmethod
    def open(cls, path: str | Path) -> JsonlLog:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return cls(target.open("w", encoding="utf-8"))

    def write(self, record: Mapping[str, Any]) -> None:
        self._stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
        self._stream.write("\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


@dataclass(slots=True)
class NodeCounters:
    claims_created: int = 0
    claims_renewed: int = 0
    releases_created: int = 0
    completions_created: int = 0
    stand_downs: int = 0
    reconciliations: int = 0
    heartbeats_sent: int = 0
    heartbeats_applied: int = 0
    heartbeats_reordered: int = 0
    heartbeats_rejected: int = 0
    evidence_applied: int = 0
    evidence_rejected: int = 0
    votes_sent: int = 0
    declarations_made: int = 0
    declarations_applied: int = 0
    retractions: int = 0
    rejections: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "rejections"
        }
        values["rejections"] = dict(sorted(self.rejections.items()))
        return values


class FieldNode:
    """One UAV: local stores, local clock, real datagrams in and out."""

    #: Read by any caller that produces published numbers. Always False.
    deterministic = False

    def __init__(
        self,
        mission: FieldMission,
        agent_id: int,
        *,
        transport: UdpTransport,
        clock: Callable[[], float],
        log: JsonlLog,
        faults: FaultInjector,
        vehicle: FieldVehicle | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.mission = mission
        self.agent_id = agent_id
        agent = mission.agent(agent_id)
        timing = mission.timing
        self.timing = timing
        self._transport = transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._log = log
        self._faults = faults
        self._vehicle: FieldVehicle = (
            PointMassVehicle(agent.start, mission.speed) if vehicle is None else vehicle
        )
        self._objectives: tuple[TaskObjective, ...] = mission.objectives
        self._objectives_by_id = {item.task_id: item for item in self._objectives}
        self._utility = ReceiverLocalTaskUtility()
        peers = tuple(a for a in mission.agent_ids if a != agent_id)
        self.peer_store = PeerStateStore(agent_id, peers, timing.peer_stale_after)
        self.claim_store = TaskClaimStore(
            agent_id,
            mission.agent_ids,
            mission.task_ids,
            timing.lease_timeout,
            freshness_timeout=timing.freshness_timeout,
        )
        self.detector = LocalFailureDetector(
            self.peer_store,
            agent_ids=mission.agent_ids,
            heartbeat_timeout=timing.heartbeat_timeout,
        )
        self.relay = EvidenceRelay(retention=timing.lease_timeout)
        self.counters = NodeCounters()
        #: Execution intent. Authorizes nothing; owns_task() is checked every use.
        self.current_task: int | None = None
        self._pending_claims: dict[int, float] = {}
        self._suspected: frozenset[int] = frozenset()
        self._logged_ownership: dict[str, dict[str, Any]] | None = None
        self._logged_peers: dict[str, str] | None = None
        self._seq = 0
        self._next_heartbeat = 0.0
        self._next_rebroadcast = 0.0
        self._next_tick = 0.0
        self._last_motion: float | None = None
        self.stopped = False
        self.fail_stopped = False
        self._started = False

    # --- logging -------------------------------------------------------

    def _snapshot(self, now: float) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        ownership: dict[str, dict[str, Any]] = {}
        for task_id in self.claim_store.task_ids:
            view = self.claim_store.view(task_id, now)
            ownership[str(task_id)] = {
                "state": view.state.value,
                "owner": view.known_owner_agent_id,
            }
        peers = {
            str(peer): self.peer_store.status_for(peer).value
            for peer in self.peer_store.peer_agent_ids
        }
        return ownership, peers

    def _record(
        self,
        kind: str,
        now: float,
        *,
        source_t: float | None = None,
        **extra: Any,
    ) -> None:
        ownership, peers = self._snapshot(now)
        self._logged_ownership = ownership
        self._logged_peers = peers
        self._seq += 1
        position = self._vehicle.position
        record: dict[str, Any] = {
            "node": self.agent_id,
            "seq": self._seq,
            "local_t": now,
            "wall": self._wall_clock(),
            "source_t": source_t,
            "kind": kind,
            "position": [position[0], position[1]],
            "current_task": self.current_task,
            "ownership": ownership,
            "peers": peers,
        }
        record.update(extra)
        self._log.write(record)

    def _log_belief_changes(self, now: float) -> None:
        ownership, peers = self._snapshot(now)
        if ownership == self._logged_ownership and peers == self._logged_peers:
            return
        changed_tasks = sorted(
            (
                int(task)
                for task, view in ownership.items()
                if self._logged_ownership is None
                or self._logged_ownership.get(task) != view
            ),
        )
        changed_peers = sorted(
            int(peer)
            for peer, status in peers.items()
            if self._logged_peers is None or self._logged_peers.get(peer) != status
        )
        self._record(
            "BELIEF",
            now,
            changed_tasks=changed_tasks,
            changed_peers=changed_peers,
        )

    def _reject(self, reason: str, now: float, detail: str) -> None:
        count = self.counters.rejections.get(reason, 0) + 1
        self.counters.rejections[reason] = count
        if count <= _REJECTION_LOG_LIMIT:
            self._record("REJECTED", now, reason=reason, detail=detail[:200])

    def on_transport_reject(self, reason: str, detail: str) -> None:
        """Hook for :class:`UdpTransport` so malformed input is counted and logged."""

        now = max(self._clock(), self.claim_store.last_timestamp or 0.0)
        self._reject(reason, now, detail)

    # --- outgoing ------------------------------------------------------

    def _send_own(self, message: FieldMessage, now: float) -> None:
        if not isinstance(message, Heartbeat):
            self.relay.remember_own(message, now)
        self._transport.send(message)

    # --- incoming ------------------------------------------------------

    def _handle(self, item: ReceivedMessage) -> None:
        message = item.message
        received_at = item.received_at
        if isinstance(message, Heartbeat):
            self._apply_heartbeat(message, item.sender, received_at)
            return
        if not self.relay.admit(message, received_at):
            return
        try:
            self._apply_evidence(message, received_at)
        except (ValueError, KeyError) as error:
            # evidence this store refuses is not forwarded either.
            self.counters.evidence_rejected += 1
            self._reject(
                "invalid_evidence", received_at, f"{type(error).__name__}: {error}"
            )
            return
        self.counters.evidence_applied += 1
        # first delivery here: forward once. Re-offers handle later bridges.
        self._transport.send(message)
        self.relay.counters.forwarded += 1

    def _apply_heartbeat(
        self, heartbeat: Heartbeat, sender: int, received_at: float
    ) -> None:
        if heartbeat.agent_id != sender:
            # receipt must mean first-hand contact; a relayed heartbeat is a
            # protocol violation, never evidence.
            self.counters.heartbeats_rejected += 1
            self._reject(
                "forwarded_heartbeat",
                received_at,
                f"heartbeat of {heartbeat.agent_id} sent by {sender}",
            )
            return
        previous = self.peer_store.observation_for(heartbeat.agent_id)
        if previous is not None and heartbeat.timestamp < previous.snapshot.timestamp:
            # UDP may reorder; an older snapshot is simply superseded.
            self.counters.heartbeats_reordered += 1
            return
        was_declared = heartbeat.agent_id in self.peer_store.declared_failed_peer_ids
        self.peer_store.receive(heartbeat, received_at)
        self.counters.heartbeats_applied += 1
        if was_declared and heartbeat.agent_id in self.peer_store.retracted_peer_ids:
            self.detector.forget(heartbeat.agent_id)
            self.counters.retractions += 1
            self._record(
                "FAILURE_RETRACTED",
                received_at,
                source_t=heartbeat.timestamp,
                peer=heartbeat.agent_id,
            )

    def _apply_evidence(self, message: FieldMessage, received_at: float) -> None:
        store = self.claim_store
        if isinstance(message, TaskClaim):
            store.receive_claim(message, received_at)
        elif isinstance(message, TaskClaimRelease):
            store.receive_release(message, received_at)
        elif isinstance(message, TaskCompletionEvidence):
            if store.receive_completion(message, received_at):
                self._record(
                    "COMPLETION_RECEIVED",
                    received_at,
                    source_t=message.created_at,
                    task=message.task_id,
                    completer=message.owner_agent_id,
                )
        elif isinstance(message, FailureVote):
            self.detector.record_vote(message, received_at=received_at)
        elif isinstance(message, FailureDeclaration):
            if self.detector.receive_declaration(message):
                self.counters.declarations_applied += 1
                self._record(
                    "FAILURE_DECLARATION_APPLIED",
                    received_at,
                    source_t=message.detected_at,
                    peer=message.agent_id,
                    declarer=message.declarer_agent_id,
                    voters=list(message.voter_agent_ids),
                    required_votes=message.required_votes,
                )

    # --- control rounds --------------------------------------------------

    def _heartbeat_round(self, now: float) -> None:
        position = self._vehicle.position
        heartbeat = Heartbeat(
            agent_id=self.agent_id,
            position=position,
            status=AgentStatus.IDLE
            if self.current_task is None
            else AgentStatus.ACTIVE,
            current_task=self.current_task,
            timestamp=now,
        )
        self._send_own(heartbeat, now)
        self.counters.heartbeats_sent += 1
        votes = self.detector.propose_votes(now)
        for vote in votes:
            self._send_own(vote, now)
        self.counters.votes_sent += len(votes)
        suspected = frozenset(vote.suspected_agent_id for vote in votes)
        if suspected != self._suspected:
            self._suspected = suspected
            self._record("SUSPICION", now, suspected=sorted(suspected))
        for declaration in self.detector.detect(now):
            self.counters.declarations_made += 1
            self._send_own(declaration, now)
            self._record(
                "FAILURE_DECLARED",
                now,
                source_t=declaration.last_heartbeat,
                peer=declaration.agent_id,
                voters=list(declaration.voter_agent_ids),
                required_votes=declaration.required_votes,
            )
        self._next_heartbeat = now + self.timing.heartbeat_interval

    def _owned_task_ids(self, now: float) -> tuple[int, ...]:
        return tuple(
            task_id
            for task_id in self.claim_store.task_ids
            if self.claim_store.view(task_id, now).state
            is TaskOwnershipState.OWNED_BY_SELF
        )

    def _task_round(self, now: float) -> None:
        store = self.claim_store
        store.advance_time(now)
        local_losses: set[int] = set()
        for decision in store.reconcile_all(now):
            self.counters.reconciliations += 1
            release = decision.local_release
            if release is not None:
                local_losses.add(decision.task_id)
                self.counters.releases_created += 1
                self._send_own(release, now)
            self._record(
                "RECONCILED",
                now,
                task=decision.task_id,
                winner=decision.winner.owner_agent_id,
                losers=[claim.owner_agent_id for claim in decision.losers],
                yielded=release is not None,
            )

        task_id = self.current_task
        if task_id is not None and not store.owns_task(task_id, now):
            self.current_task = None
            self._vehicle.hold()
            self.counters.stand_downs += 1
            self._record(
                "TASK_STOOD_DOWN",
                now,
                task=task_id,
                contested=task_id in local_losses,
                state=store.view(task_id, now).state.value,
            )

        for pending in tuple(self._pending_claims):
            if not store.owns_task(pending, now):
                self._pending_claims.pop(pending)

        task_id = self.current_task
        if task_id is not None:
            view = store.view(task_id, now)
            if view.claim_age is not None and view.claim_age >= store.freshness_timeout:
                claim = store.renew_claim(task_id, now)
                self.counters.claims_renewed += 1
                self._send_own(claim, now)
            return

        owned = self._owned_task_ids(now)
        if owned:
            self._bind_owned(owned, now)
            return
        if all(
            store.view(t, now).state is TaskOwnershipState.COMPLETE
            for t in store.task_ids
        ):
            return
        candidates = tuple(
            objective
            for objective in self._objectives
            if store.can_create_claim(objective.task_id, now)
        )
        ranked = self._utility.rank(self.agent_id, self._vehicle.position, candidates)
        if not ranked:
            return
        best = ranked[0]
        claim = store.create_claim(best.task_id, now)
        self.counters.claims_created += 1
        self._pending_claims[best.task_id] = now
        self._send_own(claim, now)
        self._record(
            "TASK_CLAIMED",
            now,
            source_t=claim.created_at,
            task=best.task_id,
            epoch=claim.epoch,
            cost=best.total_cost,
        )

    def _bind_owned(self, owned: Sequence[int], now: float) -> None:
        objectives = tuple(self._objectives_by_id[task_id] for task_id in owned)
        ranked = self._utility.rank(self.agent_id, self._vehicle.position, objectives)
        selected = ranked[0].task_id
        created = self._pending_claims.get(selected)
        if created is not None and now - created < self.timing.claim_settle:
            # give a simultaneous competing claim time to arrive and reconcile
            # before any physical action is taken.
            return
        for surplus in ranked[1:]:
            release = self.claim_store.release_claim(surplus.task_id, now)
            self.counters.releases_created += 1
            self._send_own(release, now)
            self._pending_claims.pop(surplus.task_id, None)
        self._pending_claims.pop(selected, None)
        self.current_task = selected
        self._record("TASK_BOUND", now, task=selected)

    def _motion(self, now: float) -> None:
        previous = self._last_motion
        self._last_motion = now
        elapsed = 0.0 if previous is None else now - previous
        task_id = self.current_task
        if task_id is None:
            return
        # the single authorization gate, checked at the moment of action.
        if not self.claim_store.owns_task(task_id, now):
            return
        target = self._objectives_by_id[task_id].position
        position = self._vehicle.move_toward(target, elapsed)
        if math.hypot(target[0] - position[0], target[1] - position[1]) > (
            self.mission.completion_tolerance
        ):
            return
        evidence = self.claim_store.create_completion(task_id, now)
        self.counters.completions_created += 1
        self._send_own(evidence, now)
        self.current_task = None
        self._record(
            "TASK_COMPLETED",
            now,
            source_t=evidence.created_at,
            task=task_id,
            epoch=evidence.claim.epoch,
        )

    def _rebroadcast(self, now: float) -> None:
        store = self.claim_store
        for claim in store.claims_for_broadcast(now):
            self._transport.send(claim)
        for release in store.releases_for_broadcast():
            # own clock domain: a release outlives its lease only as audit.
            if now - release.created_at <= store.lease_timeout:
                self._transport.send(release)
        for completion in store.completions_for_broadcast():
            self._transport.send(completion)
        for declaration in self.detector.declarations_for_broadcast():
            self._transport.send(declaration)
        for message in self.relay.reoffer(now):
            if origin_agent_id(message) != self.agent_id:
                self._transport.send(message)
        self._next_rebroadcast = now + self.timing.rebroadcast_interval

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        now = self._clock()
        self._started = True
        self._last_motion = now
        self._record(
            "NODE_START",
            now,
            version=__version__,
            mission_id=self.mission.mission_id,
            agents=list(self.mission.agent_ids),
            tasks=list(self.mission.task_ids),
            timing={
                "heartbeat_interval": self.timing.heartbeat_interval,
                "peer_stale_after": self.timing.peer_stale_after,
                "heartbeat_timeout": self.timing.heartbeat_timeout,
                "lease_timeout": self.timing.lease_timeout,
                "freshness_timeout": self.timing.freshness_timeout,
                "round_interval": self.timing.round_interval,
                "rebroadcast_interval": self.timing.rebroadcast_interval,
                "claim_settle": self.timing.claim_settle,
            },
            required_votes=self.detector.required_votes,
            deterministic=self.deterministic,
            vehicle_deterministic=self._vehicle.deterministic,
        )

    def step(self) -> bool:
        """Run one loop iteration; return ``False`` once the node has stopped."""

        if self.stopped:
            return False
        if not self._started:
            self.start()
        now = self._clock()
        if self._faults.fail_stop_due(now):
            self.fail_stopped = True
            self.stopped = True
            self._vehicle.hold()
            self._record("FAIL_STOP", now)
            return False
        if self._faults.refresh(now):
            self._record("FILTER_CHANGED", now, blocked=sorted(self._faults.blocked))
        for item in self._transport.receive_available():
            self._handle(item)
        now = max(self._clock(), self.claim_store.last_timestamp or 0.0)
        # newly stale peers surface through the BELIEF record below.
        self.peer_store.advance_time(now)
        if now >= self._next_heartbeat:
            self._heartbeat_round(now)
        self._task_round(now)
        self._motion(now)
        if now >= self._next_rebroadcast:
            self._rebroadcast(now)
        self._log_belief_changes(now)
        if now >= self._next_tick:
            self._record(
                "TICK",
                now,
                counters=self.counters.as_dict(),
                transport=self._transport.counters.as_dict(),
                relay=self.relay.counters.as_dict(),
                random_drops=self._faults.random_drops,
            )
            self._next_tick = now + 1.0
        return True

    def next_deadline(self) -> float:
        """The next instant this node has scheduled work, on its own clock."""

        return min(self._next_heartbeat, self._next_rebroadcast, self._next_tick)

    def stop(self, reason: str) -> None:
        now = max(self._clock(), self.claim_store.last_timestamp or 0.0)
        self._record(
            "NODE_STOP",
            now,
            reason=reason,
            counters=self.counters.as_dict(),
            transport=self._transport.counters.as_dict(),
            relay=self.relay.counters.as_dict(),
            random_drops=self._faults.random_drops,
            control_errors=self._faults.control_errors,
        )
        self.stopped = True

    def run(
        self,
        duration: float,
        *,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> str:  # pragma: no cover - exercised by the subprocess end-to-end test
        """Loop until ``duration`` seconds of node time, a signal, or fail-stop."""

        reason = "duration"
        while True:
            if should_stop():
                reason = "signal"
                break
            if self._clock() >= duration:
                break
            if not self.step():
                reason = "fail_stop"
                break
            wait = min(
                self.timing.round_interval,
                max(0.0, self.next_deadline() - self._clock()),
            )
            self._transport.wait(wait)
        self.stop(reason)
        return reason


def build_transport(
    sock: DatagramSocket,
    mission: FieldMission,
    agent_id: int,
    *,
    clock: Callable[[], float],
    faults: FaultInjector,
    destinations: Mapping[int, Address] | None = None,
) -> UdpTransport:
    """Wire a transport to the mission's addressing and the node's drop filter."""

    if mission.network.mode == "broadcast":
        return UdpTransport(
            sock,
            agent_id=agent_id,
            mission_id=mission.mission_id,
            known_agent_ids=mission.agent_ids,
            clock=clock,
            broadcast_address=mission.network.broadcast_address,
            drop_rx=faults.drops,
        )
    resolved: dict[int, Address] = {}
    for agent in mission.agents:
        if agent.agent_id == agent_id:
            continue
        address = (destinations or {}).get(agent.agent_id, agent.address)
        if address is None:
            raise ValueError(
                f"unicast mode needs an address for agent {agent.agent_id}"
            )
        resolved[agent.agent_id] = address
    return UdpTransport(
        sock,
        agent_id=agent_id,
        mission_id=mission.mission_id,
        known_agent_ids=mission.agent_ids,
        clock=clock,
        destinations=resolved,
        drop_rx=faults.drops,
        drop_tx=faults.drops,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eudis_swarm.field_node",
        description=(
            "Run one UAV of the swarm as a real UDP node (lab validation; "
            "not deterministic)."
        ),
    )
    parser.add_argument("--mission", required=True, help="shared mission JSON file")
    parser.add_argument("--agent", required=True, type=int, help="this node's agent id")
    parser.add_argument("--log", help="JSONL log path (default field-node-<id>.jsonl)")
    parser.add_argument(
        "--duration", type=float, default=60.0, help="seconds to run (default 60)"
    )
    parser.add_argument("--bind", help="HOST:PORT to bind (default from mission)")
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="ID=HOST:PORT",
        help="override one peer's unicast address (repeatable)",
    )
    parser.add_argument(
        "--drop",
        action="append",
        default=[],
        metavar="PEERS@START-END",
        help="drop traffic to/from peers in a window of node time, e.g. 3,4@5-15",
    )
    parser.add_argument(
        "--control", help="control file polled for partitions/fail-stop"
    )
    parser.add_argument(
        "--loss", type=float, default=0.0, help="extra random datagram loss in [0,1)"
    )
    parser.add_argument("--loss-seed", type=int, default=0)
    parser.add_argument(
        "--fail-stop-at", type=float, help="stop participating at this node time"
    )
    parser.add_argument(
        "--start-at",
        type=float,
        help="wall-clock UNIX time to start the loop (harness alignment only)",
    )
    parser.add_argument(
        "--mavlink", help="MAVLink connection string for a live vehicle"
    )
    parser.add_argument(
        "--takeoff-alt", type=float, help="arm and take off to this altitude (m)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - subprocess
    args = _parser().parse_args(argv)
    mission = load_mission(args.mission)
    agent = mission.agent(args.agent)
    overrides: dict[int, Address] = {}
    for item in args.peer:
        peer_text, _, address_text = item.partition("=")
        overrides[int(peer_text)] = parse_address(address_text)
    broadcast = mission.network.mode == "broadcast"
    if args.bind is not None:
        bind = parse_address(args.bind)
    elif broadcast:
        assert mission.network.broadcast_address is not None
        bind = ("0.0.0.0", mission.network.broadcast_address[1])
    elif args.agent in overrides:
        bind = overrides[args.agent]
    elif agent.address is not None:
        bind = agent.address
    else:
        raise SystemExit("no bind address: give --bind or an agent address")

    stop_requested = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    if args.start_at is not None:
        delay = args.start_at - time.time()
        if delay > 0.0:
            time.sleep(delay)
    origin = time.monotonic()

    def clock() -> float:
        return time.monotonic() - origin

    faults = FaultInjector(
        args.agent,
        windows=[parse_drop_window(text) for text in args.drop],
        control_path=args.control,
        loss=args.loss,
        loss_seed=args.loss_seed,
        fail_stop_at=args.fail_stop_at,
    )
    vehicle: FieldVehicle | None = None
    if args.mavlink:
        vehicle = open_mavlink_vehicle(args.mavlink, agent.start, args.takeoff_alt)
    sock = open_udp_socket(bind, broadcast=broadcast)
    transport = build_transport(
        sock, mission, args.agent, clock=clock, faults=faults, destinations=overrides
    )
    log = JsonlLog.open(args.log or f"field-node-{args.agent}.jsonl")
    node = FieldNode(
        mission,
        args.agent,
        transport=transport,
        clock=clock,
        log=log,
        faults=faults,
        vehicle=vehicle,
    )
    transport.set_reject_hook(node.on_transport_reject)
    try:
        reason = node.run(args.duration, should_stop=lambda: stop_requested)
    finally:
        transport.close()
        log.close()
    print(f"field node {args.agent}: stopped ({reason})", file=sys.stderr)
    return 0


__all__ = [
    "FieldNode",
    "FieldVehicle",
    "JsonlLog",
    "LOG_FORMAT",
    "LOG_VERSION",
    "MavlinkFieldVehicle",
    "NodeCounters",
    "PointMassVehicle",
    "build_transport",
    "main",
    "open_mavlink_vehicle",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
