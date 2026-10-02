"""Inject lab faults into one field node: drop filters, partitions, fail-stop.

On hardware a partition is real: switch a Raspberry Pi's WiFi off, or walk it
out of range. On one laptop every node shares the loopback interface, so a
partition has to be imposed. :class:`FaultInjector` does that at the edge of
the node, *below* the protocol: a filtered datagram is discarded exactly as if
the radio had lost it, before decoding, so the coordination logic cannot tell
an injected partition from a real one and is never told about it.

Three sources compose (a peer is blocked if any of them blocks it):

* **time windows** -- ``DropWindow(start, end, peers)`` in the node's own
  monotonic seconds since start (``--drop 3,4@5-15`` on the command line);
* **a control file** -- a small JSON document the launcher (or an operator)
  rewrites atomically while the run is live; the node re-reads it when its
  modification time changes::

      {"partition": [[1, 2], [3, 4]]}       # nodes in different groups blocked
      {"blocked_links": [[1, 3], [2, 4]]}   # explicit undirected links
      {"fail_stop": [4]}                    # these nodes stop participating

* **random loss** -- independent per-datagram drop probability with its own
  seeded generator, for exercising lossy links on loopback.

A fail-stop (control file, or ``fail_stop_at``) makes the node stop sending,
receiving and moving; the node process then exits. Recording that it happened
is observer truth for the report; peers learn nothing except silence.

This module is a test harness. Nothing in it reaches any decision logic other
than by withholding datagrams.
"""

from __future__ import annotations

import json
import os
import random
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FaultSpecError(ValueError):
    """A drop window, control file or fault schedule is malformed."""


@dataclass(frozen=True, slots=True)
class DropWindow:
    """Drop everything to and from ``peers`` while ``start <= t < end``."""

    start: float
    end: float
    peers: frozenset[int]

    def __post_init__(self) -> None:
        if not 0.0 <= self.start < self.end:
            raise FaultSpecError("drop window needs 0 <= start < end")
        if not self.peers:
            raise FaultSpecError("drop window needs at least one peer")

    def active(self, now: float) -> bool:
        return self.start <= now < self.end


def parse_drop_window(text: str) -> DropWindow:
    """Parse ``"3,4@5-15"``: peers 3 and 4, from t=5 s to t=15 s."""

    peers_text, separator, window_text = text.partition("@")
    start_text, dash, end_text = window_text.partition("-")
    if not separator or not dash:
        raise FaultSpecError(f"drop window must look like 3,4@5-15, got {text!r}")
    try:
        peers = frozenset(int(item) for item in peers_text.split(",") if item)
        start = float(start_text)
        end = float(end_text)
    except ValueError:
        raise FaultSpecError(f"drop window must look like 3,4@5-15, got {text!r}")
    return DropWindow(start=start, end=end, peers=peers)


@dataclass(frozen=True, slots=True)
class ControlState:
    """The decoded content of a control file."""

    blocked_links: frozenset[frozenset[int]] = frozenset()
    fail_stop: frozenset[int] = frozenset()

    def blocked_peers_of(self, agent_id: int) -> frozenset[int]:
        return frozenset(
            peer
            for link in self.blocked_links
            if agent_id in link
            for peer in link
            if peer != agent_id
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "blocked_links": sorted(sorted(link) for link in self.blocked_links),
            "fail_stop": sorted(self.fail_stop),
        }


def _id_list(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ):
        raise FaultSpecError(f"{name} must be a list of positive integers")
    return list(value)


def partition_links(groups: Iterable[Iterable[int]]) -> frozenset[frozenset[int]]:
    """Return every undirected link between nodes in different groups."""

    group_list = [frozenset(group) for group in groups]
    links: set[frozenset[int]] = set()
    for index, left in enumerate(group_list):
        for right in group_list[index + 1 :]:
            for a in left:
                for b in right:
                    if a != b:
                        links.add(frozenset((a, b)))
    return frozenset(links)


def parse_control(raw: object) -> ControlState:
    """Validate one decoded control document."""

    if not isinstance(raw, dict):
        raise FaultSpecError("control file must be a JSON object")
    unknown = sorted(set(raw) - {"partition", "blocked_links", "fail_stop"})
    if unknown:
        raise FaultSpecError(f"control file has unknown keys {unknown}")
    links: set[frozenset[int]] = set()
    partition = raw.get("partition")
    if partition is not None:
        if not isinstance(partition, list):
            raise FaultSpecError("partition must be a list of groups")
        links |= partition_links(
            _id_list(group, "partition group") for group in partition
        )
    blocked = raw.get("blocked_links")
    if blocked is not None:
        if not isinstance(blocked, list):
            raise FaultSpecError("blocked_links must be a list of pairs")
        for pair in blocked:
            ids = _id_list(pair, "blocked link")
            if len(ids) != 2 or ids[0] == ids[1]:
                raise FaultSpecError("a blocked link names two different nodes")
            links.add(frozenset(ids))
    fail_stop = frozenset(_id_list(raw.get("fail_stop", []), "fail_stop"))
    return ControlState(blocked_links=frozenset(links), fail_stop=fail_stop)


def write_control(path: str | Path, state: ControlState) -> None:
    """Replace a control file atomically, so a reader never sees half of it."""

    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(state.to_json()), encoding="utf-8")
    os.replace(temporary, target)


class FaultInjector:
    """Decide, per datagram and per instant, what this node's radio loses."""

    def __init__(
        self,
        agent_id: int,
        *,
        windows: Iterable[DropWindow] = (),
        control_path: str | Path | None = None,
        loss: float = 0.0,
        loss_seed: int = 0,
        fail_stop_at: float | None = None,
    ) -> None:
        if not 0.0 <= loss < 1.0:
            raise FaultSpecError("loss must be in [0, 1)")
        if fail_stop_at is not None and fail_stop_at < 0.0:
            raise FaultSpecError("fail_stop_at must be non-negative")
        self._agent_id = agent_id
        self._windows = tuple(windows)
        self._control_path = None if control_path is None else Path(control_path)
        self._control_text: str | None = None
        self._control = ControlState()
        self._loss = loss
        # a private generator: this randomness models the radio, never a choice.
        self._rng = random.Random(loss_seed)
        self._fail_stop_at = fail_stop_at
        self._blocked: frozenset[int] = frozenset()
        self.control_errors = 0
        self.random_drops = 0

    @property
    def blocked(self) -> frozenset[int]:
        """Peers blocked as of the last :meth:`refresh`."""

        return self._blocked

    def refresh(self, now: float) -> bool:
        """Re-evaluate windows and the control file; return whether blocking changed."""

        self._reload_control()
        blocked = set(self._control.blocked_peers_of(self._agent_id))
        for window in self._windows:
            if window.active(now):
                blocked |= window.peers
        blocked.discard(self._agent_id)
        new_blocked = frozenset(blocked)
        changed = new_blocked != self._blocked
        self._blocked = new_blocked
        return changed

    def fail_stop_due(self, now: float) -> bool:
        if self._fail_stop_at is not None and now >= self._fail_stop_at:
            return True
        return self._agent_id in self._control.fail_stop

    def drops(self, peer_agent_id: int) -> bool:
        """Whether one datagram to or from this peer is lost now."""

        if peer_agent_id in self._blocked:
            return True
        if self._loss > 0.0 and self._rng.random() < self._loss:
            self.random_drops += 1
            return True
        return False

    def _reload_control(self) -> None:
        if self._control_path is None:
            return
        try:
            text = self._control_path.read_text(encoding="utf-8")
        except OSError:
            # not written yet, or mid-replace on a platform without atomic
            # rename semantics: keep the previous state.
            return
        # compare content rather than mtime: coarse filesystem timestamps
        # could otherwise hide a partition and heal written in one second.
        if text == self._control_text:
            return
        try:
            state = parse_control(json.loads(text))
        except ValueError:
            # keep the previous state: a bad edit must not heal a partition.
            self.control_errors += 1
            return
        self._control_text = text
        self._control = state


@dataclass(frozen=True, slots=True)
class ScheduledFault:
    """One launcher-side event, at seconds after the common start."""

    at: float
    state: ControlState
    label: str


def load_fault_schedule(path: str | Path) -> tuple[ScheduledFault, ...]:
    """Read a launcher fault schedule into cumulative control states.

    Format::

        {"events": [{"at": 4.0, "partition": [[1, 2], [3, 4]]},
                    {"at": 9.0, "heal": true},
                    {"at": 12.0, "fail_stop": [4]}]}

    ``partition``/``blocked_links`` replace the current blocking; ``heal``
    clears it; ``fail_stop`` adds nodes to the stopped set (which is never
    undone -- fail-stop is permanent).
    """

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FaultSpecError(f"cannot read fault schedule {path}: {error}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        raise FaultSpecError("fault schedule must be an object with an events list")
    events = raw["events"]
    blocked: frozenset[frozenset[int]] = frozenset()
    stopped: frozenset[int] = frozenset()
    schedule: list[ScheduledFault] = []
    previous_at = 0.0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise FaultSpecError(f"events[{index}] must be an object")
        unknown = sorted(
            set(event) - {"at", "partition", "blocked_links", "heal", "fail_stop"}
        )
        if unknown:
            raise FaultSpecError(f"events[{index}] has unknown keys {unknown}")
        at = event.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool) or at < previous_at:
            raise FaultSpecError("event times must be non-decreasing numbers >= 0")
        previous_at = float(at)
        labels: list[str] = []
        if event.get("heal"):
            blocked = frozenset()
            labels.append("heal")
        if "partition" in event or "blocked_links" in event:
            parsed = parse_control(
                {
                    key: event[key]
                    for key in ("partition", "blocked_links")
                    if key in event
                }
            )
            blocked = parsed.blocked_links
            labels.append("partition")
        if "fail_stop" in event:
            stopped = stopped | frozenset(_id_list(event["fail_stop"], "fail_stop"))
            labels.append("fail_stop")
        if not labels:
            raise FaultSpecError(f"events[{index}] does nothing")
        schedule.append(
            ScheduledFault(
                at=float(at),
                state=ControlState(blocked_links=blocked, fail_stop=stopped),
                label="+".join(labels),
            )
        )
    return tuple(schedule)


__all__ = [
    "ControlState",
    "DropWindow",
    "FaultInjector",
    "FaultSpecError",
    "ScheduledFault",
    "load_fault_schedule",
    "parse_control",
    "parse_drop_window",
    "partition_links",
    "write_control",
]
