"""Central configuration for the deterministic swarm simulation.

Every knob the simulator has lives on ``SimulationConfig``. Read it as a table
of contents for the whole system: each field names a behaviour, and the module
that consumes it is noted below. The dataclass is ``frozen`` -- a run cannot
reconfigure itself midway -- and ``__post_init__`` rejects any combination that
would make a run ill-defined rather than letting it fail obscurely later.

The fields fall into five groups:

* **scenario size and geometry** -- how many UAVs and tasks, the play area, and
  the seed that places the tasks;
* **motion and mission timing** -- speed, the fixed simulator step, and how
  close counts as "arrived";
* **failure detection** -- the heartbeat cadence and the silence thresholds
  that feed :mod:`eudis_swarm.peer_state` and
  :mod:`eudis_swarm.failure_manager`;
* **control model** -- ``allocation_policy`` / ``task_control`` select
  distributed claim ownership or the centralized-coordinator baseline, plus the
  optional versioned :class:`~eudis_swarm.mission_contract.MissionContract`;
* **communication impairment** -- the link model, an optional radio model,
  statically blocked links, a region jammer, and a per-UAV comms outage window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .communication import RadioModel, RegionJammer
from .mission_contract import MissionContract
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_positive_real,
)


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Validated parameters for a deterministic simulation run."""

    # --- scenario size and geometry ---
    #: Number of UAVs. IDs are 1..agent_count. The first four start in the area
    #: corners; any beyond that go on a ring (simulation._initial_positions).
    agent_count: int = 4
    #: Number of mission tasks. IDs are 1..task_count.
    task_count: int = 20
    #: Seed for the one RNG in the build: task placement only. Nothing else in
    #: the simulator uses randomness unless ``stochastic_delivery`` is set.
    random_seed: int = 2026
    #: Play-area size in abstract metres. Task positions are sampled inside a
    #: 10% margin; UAV corner positions sit in a 5% margin.
    area_width: float = 100.0
    area_height: float = 100.0

    # --- motion and mission timing ---
    #: UAV ground speed (metres per simulated second), identical for every UAV.
    agent_speed: float = 10.0
    #: Fixed spacing of motion/mission boundaries. The event loop still jumps
    #: straight to the next scheduled event; this only sets the motion cadence.
    time_step: float = 0.25
    #: Distance within which a moving UAV counts as having reached its task.
    completion_tolerance: float = 0.75

    # --- failure detection (feeds peer_state + failure_manager) ---
    #: How often each UAV emits a heartbeat snapshot.
    heartbeat_interval: float = 1.0
    #: Receiver-local silence age after which a detector replica may vote a peer
    #: failed. Must be >= heartbeat_interval.
    failure_timeout: float = 2.5
    #: Receiver-local age after which a delivered peer snapshot is treated as
    #: STALE rather than FRESH in PeerStateStore.
    peer_state_stale_after: float = 2.5

    # --- control model ---
    #: Scoring policy for task selection: "distance" (nearest),
    #: "connectivity" (prefer tasks that keep predicted peer degree up), or
    #: "quantum" (each idle UAV solves a small joint assignment over itself and
    #: its HEARD peers with a simulated quantum solver; see quantum_opt.py).
    allocation_policy: str = "distance"
    #: With allocation_policy="quantum": "qaoa" (statevector QAOA) or "sqa"
    #: (simulated quantum annealing).
    quantum_solver: str = "qaoa"
    #: With allocation_policy="quantum": most UAVs in one formulation (self
    #: plus the nearest idle HEARD peers) and most objectives (the nearest
    #: claimable ones). The defaults give at most 9 qubits.
    quantum_max_agents: int = 3
    quantum_max_objectives: int = 3
    #: QAOA depth p for quantum_solver="qaoa".
    qaoa_depth: int = 2
    #: "distributed" replicated claim ownership (the operational model), or
    #: "centralized" -- the single-coordinator comparison baseline.
    task_control: str = "distributed"
    #: Optional operator intent briefed to every UAV before launch. ``None``
    #: means no contracted degradation policy (UAVs are unconstrained).
    mission_contract: MissionContract | None = None
    #: An amendment issued mid-mission to one UAV, which must then reach the
    #: rest as evidence. Across a partition, it will not.
    contract_amendment: MissionContract | None = None
    #: When the amendment is handed to that one UAV.
    contract_amendment_time: float = 6.0
    #: Which UAV the operator reaches with the amendment.
    contract_amendment_agent_id: int = 1
    #: Lease length for a task claim. A claim older than this (receiver-local)
    #: is EXPIRED and the task is free to re-claim; freshness_timeout defaults
    #: to half this. This is the knob the two quorum-floor presets differ on.
    claim_lease_duration: float = 2.5

    # --- injected fault schedule ---
    #: UAV made silent and immobile at ``failure_time`` (a real stop, not a
    #: declaration). Set failure_time huge to disable.
    failure_agent_id: int = 2
    failure_time: float = 4.0
    #: Hard wall-clock ceiling on simulated time; the run ends unfinished if hit.
    max_simulation_time: float = 300.0

    # --- communication impairment ---
    #: With link_model="range", the max distance for an active link. Ignored by
    #: link_model="radio".
    communication_range: float = 130.0
    #: "range" distance threshold (default) or "radio" free-space line-of-sight
    #: bit-error-rate model. Not calibrated RF -- see the module docstrings.
    link_model: str = "range"
    #: With link_model="radio", sample each link up/down from its delivery
    #: probability using ``random_seed``. Requires link_model="radio".
    stochastic_delivery: bool = False
    #: Parameters for the optional radio model (used only when link_model="radio").
    radio_model: RadioModel = field(default_factory=RadioModel)
    #: Undirected UAV-ID pairs whose link is permanently down. Normalised to
    #: sorted, deduplicated tuples in ``__post_init__``.
    blocked_links: frozenset[tuple[int, int]] = frozenset()
    #: Optional circular jamming region: links crossing the disc are down while
    #: it is active.
    region_jammer: RegionJammer | None = None
    #: One UAV fully comms-isolated for a window (distinct from a physical
    #: failure: the UAV keeps flying and its own software keeps running).
    comm_fault_agent_id: int | None = None
    comm_fault_start: float = 4.0
    comm_fault_end: float = 8.0

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_count, name="agent_count")
        validate_positive_integer(self.task_count, name="task_count")
        validate_positive_integer(self.failure_agent_id, name="failure_agent_id")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an integer")
        if self.comm_fault_agent_id is not None:
            validate_positive_integer(
                self.comm_fault_agent_id, name="comm_fault_agent_id"
            )
        if self.allocation_policy not in {"distance", "connectivity", "quantum"}:
            raise ValueError(
                "allocation_policy must be 'distance', 'connectivity' or 'quantum'"
            )
        if self.quantum_solver not in {"qaoa", "sqa"}:
            raise ValueError("quantum_solver must be 'qaoa' or 'sqa'")
        validate_positive_integer(self.quantum_max_agents, name="quantum_max_agents")
        validate_positive_integer(
            self.quantum_max_objectives, name="quantum_max_objectives"
        )
        if not isinstance(self.qaoa_depth, int) or isinstance(self.qaoa_depth, bool):
            raise ValueError("qaoa_depth must be an integer")
        if not 1 <= self.qaoa_depth <= 4:
            raise ValueError("qaoa_depth must be between 1 and 4")
        if self.allocation_policy == "quantum":
            # Bounds: the statevector simulator's qubit limit, and a solve
            # budget that keeps a run interactive.
            qubits = self.quantum_max_agents * self.quantum_max_objectives
            if self.quantum_solver == "qaoa" and qubits > 16:
                raise ValueError(
                    "quantum_max_agents * quantum_max_objectives must be <= 16 "
                    "for the QAOA simulator"
                )
            if qubits > 256:
                raise ValueError(
                    "quantum_max_agents * quantum_max_objectives must be <= 256"
                )
            if self.task_control != "distributed":
                raise ValueError(
                    "allocation_policy='quantum' requires task_control='distributed'"
                )
        if self.contract_amendment is not None:
            validate_positive_integer(
                self.contract_amendment_agent_id,
                name="contract_amendment_agent_id",
            )
            if not 1 <= self.contract_amendment_agent_id <= self.agent_count:
                raise ValueError(
                    "contract_amendment_agent_id must identify a configured agent"
                )
            if self.mission_contract is None:
                raise ValueError("an amendment requires an initial mission_contract")
            if not self.contract_amendment.supersedes(self.mission_contract):
                raise ValueError("contract_amendment must supersede mission_contract")
            validate_nonnegative_real(
                self.contract_amendment_time, name="contract_amendment_time"
            )
        if self.task_control not in {"distributed", "centralized"}:
            raise ValueError("task_control must be 'distributed' or 'centralized'")
        if self.link_model not in {"range", "radio"}:
            raise ValueError("link_model must be 'range' or 'radio'")
        if not isinstance(self.stochastic_delivery, bool):
            raise ValueError("stochastic_delivery must be a boolean")
        if not isinstance(self.radio_model, RadioModel):
            raise ValueError("radio_model must be a RadioModel instance")
        if self.stochastic_delivery and self.link_model != "radio":
            raise ValueError("stochastic_delivery requires link_model='radio'")

        try:
            supplied_blocked_links = list(self.blocked_links)
        except TypeError:
            raise ValueError(
                "blocked_links must be an iterable of UAV ID pairs"
            ) from None
        canonical_blocked_links: set[tuple[int, int]] = set()
        for pair in supplied_blocked_links:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(
                    not isinstance(endpoint, int) or isinstance(endpoint, bool)
                    for endpoint in pair
                )
            ):
                raise ValueError("blocked_links entries must be two-item UAV ID tuples")
            left_agent_id, right_agent_id = pair
            if left_agent_id == right_agent_id:
                raise ValueError("blocked_links must not contain self-links")
            if not (
                1 <= left_agent_id <= self.agent_count
                and 1 <= right_agent_id <= self.agent_count
            ):
                raise ValueError(
                    "blocked_links entries must identify configured agents"
                )
            canonical_blocked_links.add(
                (
                    min(left_agent_id, right_agent_id),
                    max(left_agent_id, right_agent_id),
                )
            )
        object.__setattr__(self, "blocked_links", frozenset(canonical_blocked_links))
        if self.region_jammer is not None and not isinstance(
            self.region_jammer, RegionJammer
        ):
            raise ValueError("region_jammer must be a RegionJammer instance")

        positive_floats = {
            "area_width": self.area_width,
            "area_height": self.area_height,
            "agent_speed": self.agent_speed,
            "time_step": self.time_step,
            "heartbeat_interval": self.heartbeat_interval,
            "failure_timeout": self.failure_timeout,
            "peer_state_stale_after": self.peer_state_stale_after,
            "claim_lease_duration": self.claim_lease_duration,
            "max_simulation_time": self.max_simulation_time,
            "communication_range": self.communication_range,
        }
        for name, value in positive_floats.items():
            validate_positive_real(value, name=name)

        non_negative_floats = {
            "completion_tolerance": self.completion_tolerance,
            "failure_time": self.failure_time,
            "comm_fault_start": self.comm_fault_start,
            "comm_fault_end": self.comm_fault_end,
        }
        for name, value in non_negative_floats.items():
            validate_nonnegative_real(value, name=name)

        if self.comm_fault_end <= self.comm_fault_start:
            raise ValueError("comm_fault_end must be greater than comm_fault_start")
        if self.failure_timeout < self.heartbeat_interval:
            raise ValueError("failure_timeout must be at least heartbeat_interval")
        if not 1 <= self.failure_agent_id <= self.agent_count:
            raise ValueError("failure_agent_id must identify a configured agent")
        if self.comm_fault_agent_id is not None and not (
            1 <= self.comm_fault_agent_id <= self.agent_count
        ):
            raise ValueError("comm_fault_agent_id must identify a configured agent")
