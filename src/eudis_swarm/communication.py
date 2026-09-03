"""Model deterministic undirected communication topology for the swarm simulator.
Links combine reachability with whole-agent and link-level faults.  Reachability
is either a binary distance threshold or a physical free-space radio model."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from math import erfc, hypot, isfinite, pi, sqrt
from numbers import Real
from typing import Iterable, Mapping

from .agent import Position
from .validation import validate_positive_real


@dataclass(frozen=True, slots=True)
class RadioModel:
    """Free-space line-of-sight radio link model.

    Implements the free-space LoS relations of Hu, Ren & Cheng
    (arXiv:2407.11531, eqs. 5-10)::

        L_ij = (4*pi*d_ij*f/c)**2 * xi_los       # path loss factor
        P_r  = EIRP * g_r / L_ij                  # received power
        SNR  = P_r / sigma**2
        BER  = 0.5 * erfc(sqrt(SNR))             # BPSK
        link up iff BER <= P_e0

    Decibel fields are converted to linear internally; power fields are treated
    in milliwatts, so ``eirp_dbm`` and ``noise_dbm`` share one linear domain.
    ``can_link`` applies the paper's hard rule; ``link_quality`` returns a
    per-frame delivery probability in ``[0, 1]`` for an optional stochastic
    delivery layer.
    """

    frequency_hz: float = 2.4e9
    eirp_dbm: float = 20.0
    rx_gain_db: float = 3.0
    xi_los_db: float = 3.0
    noise_dbm: float = -100.0
    ber_threshold: float = 1e-5
    frame_bits: int = 1024
    speed_of_light_m_s: float = 299_792_458.0

    def __post_init__(self) -> None:
        for name, value in (
            ("frequency_hz", self.frequency_hz),
            ("speed_of_light_m_s", self.speed_of_light_m_s),
        ):
            if (
                not isinstance(value, Real)
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"RadioModel {name} must be finite and positive")
        for name, value in (
            ("eirp_dbm", self.eirp_dbm),
            ("rx_gain_db", self.rx_gain_db),
            ("xi_los_db", self.xi_los_db),
            ("noise_dbm", self.noise_dbm),
        ):
            if (
                not isinstance(value, Real)
                or isinstance(value, bool)
                or not isfinite(value)
            ):
                raise ValueError(f"RadioModel {name} must be a finite decibel value")
        if (
            not isinstance(self.frame_bits, int)
            or isinstance(self.frame_bits, bool)
            or self.frame_bits <= 0
        ):
            raise ValueError("RadioModel frame_bits must be a positive integer")
        if (
            not isinstance(self.ber_threshold, Real)
            or isinstance(self.ber_threshold, bool)
            or not 0.0 < float(self.ber_threshold) < 1.0
        ):
            raise ValueError("RadioModel ber_threshold must lie in (0, 1)")

    @staticmethod
    def _db_to_linear(decibels: float) -> float:
        return 10.0 ** (decibels / 10.0)

    @property
    def _eirp_mw(self) -> float:
        return self._db_to_linear(self.eirp_dbm)

    @property
    def _rx_gain_linear(self) -> float:
        return self._db_to_linear(self.rx_gain_db)

    @property
    def _xi_los_linear(self) -> float:
        return self._db_to_linear(self.xi_los_db)

    @property
    def _noise_mw(self) -> float:
        return self._db_to_linear(self.noise_dbm)

    def _validate_distance(self, distance: float) -> float:
        if (
            not isinstance(distance, Real)
            or isinstance(distance, bool)
            or not isfinite(distance)
            or distance < 0.0
        ):
            raise ValueError("radio link distance must be finite and non-negative")
        return float(distance)

    def path_loss(self, distance: float) -> float:
        """Return the free-space LoS path loss factor ``L_ij`` (eq. 5)."""

        distance = self._validate_distance(distance)
        free_space = (
            4.0 * pi * distance * self.frequency_hz / self.speed_of_light_m_s
        ) ** 2
        return free_space * self._xi_los_linear

    def snr(self, distance: float) -> float:
        """Return the receiver signal-to-noise ratio (eqs. 6-8)."""

        loss = self.path_loss(distance)
        if loss <= 0.0:
            # co-located radios have no free-space loss.
            return float("inf")
        received_mw = self._eirp_mw * self._rx_gain_linear / loss
        return received_mw / self._noise_mw

    def bit_error_rate(self, distance: float) -> float:
        """Return the BPSK bit error rate ``0.5 * erfc(sqrt(SNR))`` (eq. 9)."""

        return 0.5 * erfc(sqrt(self.snr(distance)))

    def can_link(self, distance: float) -> bool:
        """Return whether ``BER <= ber_threshold`` for a link of that length (eq. 10)."""

        return self.bit_error_rate(distance) <= self.ber_threshold

    def link_quality(self, distance: float) -> float:
        """Return the per-frame delivery probability in ``[0, 1]``."""

        ber = self.bit_error_rate(distance)
        if ber <= 0.0:
            return 1.0
        if ber >= 1.0:
            return 0.0
        return max(0.0, min(1.0, (1.0 - ber) ** self.frame_bits))


class CommunicationState(str, Enum):
    """Whether a UAV currently has at least one available peer link."""

    REACHABLE = "REACHABLE"
    UNREACHABLE = "UNREACHABLE"


@dataclass(frozen=True, slots=True)
class CommunicationLink:
    """One canonical undirected UAV pair and its current link state."""

    source_agent_id: int
    destination_agent_id: int
    distance: float
    available: bool

    def __post_init__(self) -> None:
        if self.source_agent_id <= 0 or self.destination_agent_id <= 0:
            raise ValueError("communication link UAV IDs must be positive")
        if self.source_agent_id >= self.destination_agent_id:
            raise ValueError(
                "communication link endpoints must use increasing canonical order"
            )
        # link records are rebuilt in the graph hot path, so validation stays local.
        if not isfinite(self.distance) or self.distance < 0.0:
            raise ValueError(
                "communication link distance must be finite and non-negative"
            )
        if not isinstance(self.available, bool):
            raise TypeError("communication link availability must be boolean")

    @property
    def key(self) -> tuple[int, int]:
        """Return the canonical pair key for deterministic comparisons."""

        return (self.source_agent_id, self.destination_agent_id)


@dataclass(frozen=True, slots=True)
class CommunicationUpdate:
    """Deterministic topology differences produced by one graph update."""

    is_initial: bool
    lost_links: tuple[CommunicationLink, ...]
    restored_links: tuple[CommunicationLink, ...]
    newly_isolated_agent_ids: tuple[int, ...]
    newly_reachable_agent_ids: tuple[int, ...]
    previous_component_count: int | None
    component_count: int
    was_fully_connected: bool | None
    is_fully_connected: bool

    @property
    def network_partitioned(self) -> bool:
        """Whether this update changed a connected graph into a partition."""

        return self.was_fully_connected is True and not self.is_fully_connected

    @property
    def network_reconnected(self) -> bool:
        """Whether this update restored one fully connected component."""

        return self.was_fully_connected is False and self.is_fully_connected


class CommunicationGraph:
    """A fixed-vertex, time-varying undirected communication graph.

    The first :meth:`update` establishes the baseline topology.  It deliberately
    reports no transitions, so initial links are not counted as restorations and
    initially isolated UAVs are not counted as new isolation events.
    """

    def __init__(
        self,
        agent_ids: Iterable[int],
        communication_range: float,
        *,
        radio_model: RadioModel | None = None,
        stochastic_links: bool = False,
        link_seed: int = 0,
    ) -> None:
        supplied_ids = tuple(agent_ids)
        if not supplied_ids:
            raise ValueError("communication graph requires at least one UAV")
        if any(
            not isinstance(agent_id, int) or isinstance(agent_id, bool) or agent_id <= 0
            for agent_id in supplied_ids
        ):
            raise ValueError("communication graph UAV IDs must be positive integers")
        if len(set(supplied_ids)) != len(supplied_ids):
            raise ValueError("communication graph UAV IDs must be unique")
        self._agent_ids = tuple(sorted(supplied_ids))
        self._agent_id_set = frozenset(self._agent_ids)
        self._communication_range = validate_positive_real(
            communication_range, name="communication_range"
        )
        if radio_model is not None and not isinstance(radio_model, RadioModel):
            raise TypeError("radio_model must be a RadioModel instance")
        if not isinstance(stochastic_links, bool):
            raise TypeError("stochastic_links must be boolean")
        if stochastic_links and radio_model is None:
            raise ValueError("stochastic_links requires a radio_model")
        if not isinstance(link_seed, int) or isinstance(link_seed, bool):
            raise TypeError("link_seed must be an integer")
        self._radio_model = radio_model
        self._stochastic_links = stochastic_links
        # a dedicated stream keeps link sampling reproducible and independent of
        # any other seeded draw in the run.
        self._link_rng = random.Random(link_seed) if stochastic_links else None
        # uav membership never changes, so canonical pair order is fixed once.
        self._pair_keys = tuple(combinations(self._agent_ids, 2))
        self._pair_key_set = frozenset(self._pair_keys)
        self._blocked_agent_ids: frozenset[int] = frozenset()
        self._blocked_links: frozenset[tuple[int, int]] = frozenset()
        self._links: dict[tuple[int, int], CommunicationLink] = {}
        self._active_links: tuple[CommunicationLink, ...] = ()
        self._active_keys: frozenset[tuple[int, int]] = frozenset()
        self._neighbors: dict[int, frozenset[int]] = {}
        self._components: tuple[frozenset[int], ...] = ()
        self._isolated_agent_ids: frozenset[int] = frozenset()
        self._initialized = False

    @property
    def agent_ids(self) -> tuple[int, ...]:
        return self._agent_ids

    @property
    def communication_range(self) -> float:
        return self._communication_range

    @property
    def radio_model(self) -> RadioModel | None:
        """Return the physical link model, or ``None`` for the range threshold."""

        return self._radio_model

    @property
    def stochastic_links(self) -> bool:
        """Whether available links are sampled from their delivery probability."""

        return self._stochastic_links

    def _link_within_reach(self, distance: float) -> bool:
        """Decide raw reachability for one pair before fault policy is applied.

        Three modes: the binary distance threshold (no radio model); the
        radio model's hard ``BER <= P_e0`` rule (deterministic); or, when
        ``stochastic_links`` is set, a seeded draw against the link's
        delivery probability.  The seeded stream is consumed once per pair
        per update in fixed pair order, so identical seeds reproduce
        identical topology histories.
        """

        if self._radio_model is None:
            return distance <= self._communication_range
        if self._link_rng is None:
            return self._radio_model.can_link(distance)
        return self._link_rng.random() < self._radio_model.link_quality(distance)

    @property
    def blocked_agent_ids(self) -> frozenset[int]:
        return self._blocked_agent_ids

    @property
    def blocked_links(self) -> frozenset[tuple[int, int]]:
        """Return explicitly blocked links in canonical endpoint order."""

        return self._blocked_links

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def links(self) -> tuple[CommunicationLink, ...]:
        """Return all canonical UAV pairs, including unavailable links."""

        self._require_initialized()
        return tuple(self._links.values())

    @property
    def active_links(self) -> tuple[CommunicationLink, ...]:
        """Return only currently available links in canonical order."""

        self._require_initialized()
        return self._active_links

    @property
    def link_count(self) -> int:
        self._require_initialized()
        return len(self._active_links)

    def link_between(
        self, left_agent_id: int, right_agent_id: int
    ) -> CommunicationLink:
        """Return the current canonical pair record for two distinct UAVs."""

        self._require_initialized()
        self._require_known_agent(left_agent_id)
        self._require_known_agent(right_agent_id)
        if left_agent_id == right_agent_id:
            raise ValueError("a UAV does not have a communication link to itself")
        source_agent_id, destination_agent_id = sorted((left_agent_id, right_agent_id))
        key = (source_agent_id, destination_agent_id)
        return self._links[key]

    def can_deliver(self, source_agent_id: int, destination_agent_id: int) -> bool:
        """Return whether the undirected link currently permits delivery."""

        return self.link_between(source_agent_id, destination_agent_id).available

    def neighbors(self, agent_id: int) -> frozenset[int]:
        """Return the currently available one-hop peers for a UAV."""

        self._require_initialized()
        self._require_known_agent(agent_id)
        return self._neighbors[agent_id]

    @property
    def connected_components(self) -> tuple[frozenset[int], ...]:
        self._require_initialized()
        return self._components

    @property
    def isolated_agent_ids(self) -> frozenset[int]:
        self._require_initialized()
        return self._isolated_agent_ids

    @property
    def is_fully_connected(self) -> bool:
        self._require_initialized()
        return len(self._components) == 1

    def communication_state(self, agent_id: int) -> CommunicationState:
        """Report peer reachability independently of physical UAV state."""

        self._require_initialized()
        self._require_known_agent(agent_id)
        if agent_id in self._isolated_agent_ids:
            return CommunicationState.UNREACHABLE
        return CommunicationState.REACHABLE

    def update(
        self,
        positions: Mapping[int, Position],
        *,
        blocked_agent_ids: Iterable[int] = (),
        blocked_links: Iterable[tuple[int, int]] = (),
    ) -> CommunicationUpdate:
        """Recompute links from positions and the current explicit fault policy."""

        self._validate_positions(positions)
        blocked = frozenset(blocked_agent_ids)
        if any(
            not isinstance(agent_id, int) or isinstance(agent_id, bool)
            for agent_id in blocked
        ):
            raise ValueError("blocked_agent_ids must contain integer UAV IDs")
        unknown_blocked = blocked - self._agent_id_set
        if unknown_blocked:
            raise ValueError(
                f"blocked_agent_ids contains unknown UAV IDs: {sorted(unknown_blocked)}"
            )
        blocked_link_keys = self._normalize_blocked_links(blocked_links)

        new_links: dict[tuple[int, int], CommunicationLink] = {}
        for source_agent_id, destination_agent_id in self._pair_keys:
            source = positions[source_agent_id]
            destination = positions[destination_agent_id]
            distance = hypot(
                destination[0] - source[0],
                destination[1] - source[1],
            )
            # reachability is sampled first so the seeded stream is consumed in a
            # fixed pair order regardless of the fault policy in force.
            within_reach = self._link_within_reach(distance)
            available = (
                within_reach
                and source_agent_id not in blocked
                and destination_agent_id not in blocked
                and (source_agent_id, destination_agent_id) not in blocked_link_keys
            )
            link = CommunicationLink(
                source_agent_id=source_agent_id,
                destination_agent_id=destination_agent_id,
                distance=distance,
                available=available,
            )
            new_links[link.key] = link

        new_active_links = tuple(link for link in new_links.values() if link.available)
        new_active_keys = frozenset(link.key for link in new_active_links)
        new_neighbors, new_components = self._build_topology(new_active_keys)
        new_isolated = frozenset(
            agent_id for agent_id, peers in new_neighbors.items() if not peers
        )
        new_fully_connected = len(new_components) == 1

        if self._initialized:
            lost_links = tuple(
                new_links[key] for key in sorted(self._active_keys - new_active_keys)
            )
            restored_links = tuple(
                new_links[key] for key in sorted(new_active_keys - self._active_keys)
            )
            newly_isolated = tuple(sorted(new_isolated - self._isolated_agent_ids))
            newly_reachable = tuple(sorted(self._isolated_agent_ids - new_isolated))
            previous_component_count: int | None = len(self._components)
            was_fully_connected: bool | None = len(self._components) == 1
            is_initial = False
        else:
            lost_links = ()
            restored_links = ()
            newly_isolated = ()
            newly_reachable = ()
            previous_component_count = None
            was_fully_connected = None
            is_initial = True

        self._blocked_agent_ids = blocked
        self._blocked_links = blocked_link_keys
        self._links = new_links
        self._active_links = new_active_links
        self._active_keys = new_active_keys
        self._neighbors = new_neighbors
        self._components = new_components
        self._isolated_agent_ids = new_isolated
        self._initialized = True

        return CommunicationUpdate(
            is_initial=is_initial,
            lost_links=lost_links,
            restored_links=restored_links,
            newly_isolated_agent_ids=newly_isolated,
            newly_reachable_agent_ids=newly_reachable,
            previous_component_count=previous_component_count,
            component_count=len(new_components),
            was_fully_connected=was_fully_connected,
            is_fully_connected=new_fully_connected,
        )

    def _build_topology(
        self, active_keys: set[tuple[int, int]] | frozenset[tuple[int, int]]
    ) -> tuple[dict[int, frozenset[int]], tuple[frozenset[int], ...]]:
        mutable_neighbors: dict[int, set[int]] = {
            agent_id: set() for agent_id in self._agent_ids
        }
        for source_agent_id, destination_agent_id in active_keys:
            mutable_neighbors[source_agent_id].add(destination_agent_id)
            mutable_neighbors[destination_agent_id].add(source_agent_id)

        remaining = set(self._agent_ids)
        components: list[frozenset[int]] = []
        while remaining:
            root = min(remaining)
            stack = [root]
            component: set[int] = set()
            while stack:
                agent_id = stack.pop()
                if agent_id in component:
                    continue
                component.add(agent_id)
                stack.extend(
                    sorted(mutable_neighbors[agent_id] - component, reverse=True)
                )
            remaining -= component
            components.append(frozenset(component))

        neighbors = {
            agent_id: frozenset(peers) for agent_id, peers in mutable_neighbors.items()
        }
        return neighbors, tuple(components)

    def _normalize_blocked_links(
        self, blocked_links: Iterable[tuple[int, int]]
    ) -> frozenset[tuple[int, int]]:
        """Validate undirected pairs and return stable canonical link keys."""

        normalized: set[tuple[int, int]] = set()
        for pair in blocked_links:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("blocked_links must contain two-item UAV ID tuples")
            left_agent_id, right_agent_id = pair
            if any(
                not isinstance(agent_id, int) or isinstance(agent_id, bool)
                for agent_id in pair
            ):
                raise ValueError("blocked_links must contain integer UAV IDs")
            if left_agent_id == right_agent_id:
                raise ValueError("blocked_links must not contain self-links")
            source_agent_id, destination_agent_id = sorted(
                (left_agent_id, right_agent_id)
            )
            key = (source_agent_id, destination_agent_id)
            if key not in self._pair_key_set:
                unknown_ids = sorted(set(key) - self._agent_id_set)
                raise ValueError(
                    f"blocked_links contains unknown UAV IDs: {unknown_ids}"
                )
            normalized.add(key)
        return frozenset(normalized)

    def _validate_positions(self, positions: Mapping[int, Position]) -> None:
        supplied_ids = frozenset(positions)
        if supplied_ids != self._agent_id_set:
            missing = sorted(self._agent_id_set - supplied_ids)
            extra = sorted(supplied_ids - self._agent_id_set, key=repr)
            raise ValueError(
                "positions must contain exactly the graph UAV IDs; "
                f"missing={missing}, extra={extra}"
            )

        for agent_id in self._agent_ids:
            position = positions[agent_id]
            if len(position) != 2 or any(
                not isinstance(coordinate, Real)
                or isinstance(coordinate, bool)
                or not isfinite(coordinate)
                for coordinate in position
            ):
                raise ValueError(
                    f"position for UAV {agent_id} must contain two finite coordinates"
                )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("communication graph has not been updated")

    def _require_known_agent(self, agent_id: int) -> None:
        if (
            not isinstance(agent_id, int)
            or isinstance(agent_id, bool)
            or agent_id not in self._agent_id_set
        ):
            raise KeyError(f"unknown UAV ID {agent_id}")


__all__ = [
    "CommunicationGraph",
    "CommunicationLink",
    "CommunicationState",
    "CommunicationUpdate",
    "RadioModel",
]
