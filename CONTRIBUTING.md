# Contributing

Thanks for looking at EUDIS Swarm. This guide protects both reproducible results
and the boundary between simulated world truth and receiver-local evidence.

## The one rule that matters

The README publishes precise numbers (`17.25 s`, `4.00 -> 5.75 -> 10.75`,
`144 / 120 / 24`). Those are regression-tested. If your change moves any of
them, that is not automatically wrong — but it must be deliberate, explained in
the pull request, and the README and tests must be updated in the same commit.
A silent change to a published figure is the one failure this project cannot
tolerate.

Determinism means: no wall-clock time, no unseeded randomness, no iteration
over unordered sets where order reaches an output, and no dependence on dict
insertion order that is not itself deterministic.

## Setting up

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,visualization,dashboard]"
```

If tests fail to import `eudis_swarm`, your editable install is stale — most
often because the repository was moved. Re-run the install command above.

## Before you open a pull request

Run the full local baseline. CI runs the same checks on Python 3.11 and 3.13.

```bash
python -m ruff check . && python -m ruff format --check . && python -m pyright && python -m pytest --cov=eudis_swarm --cov-report=term-missing && python -m build
```

Coverage is gated at 85% branch-aware. Pyright runs in `standard` mode and must
report zero errors.

## Architectural boundaries

These separations are load-bearing. Breaking one is a design change, not a
refactor, and needs discussion first.

| Boundary | Rule |
| --- | --- |
| World truth vs agent belief | Physics, authoritative positions, global tasks, and topology belong to the simulation. An agent-local component may use self state and delivered/local evidence only. |
| Physical vs communication state | `IDLE`/`ACTIVE`/`FAILED` never derives directly from link state. `SILENT`, `UNREACHABLE`, `STALE`, and `UNKNOWN` must never imply `FAILED`. |
| Silence vs declaration | A timeout may create a local `FailureVote`; only a validated quorum in one replica's mailbox may create `DECLARED_FAILED` and authorize recovery. |
| Emission vs delivery | Creating a heartbeat, vote, declaration, claim, release, or completion is not remote delivery. Remote state changes must pass through the appropriate modeled transport and an available link. |
| Task truth vs ownership evidence | `TaskStatus` is authoritative world state. `TaskClaimStore` derives only the six `TaskOwnershipState` values from local protocol evidence; stale remains owned until strict lease expiry. |
| Proposal vs authority | Allocators propose `Allocation` records. Only `Mission` mutates agents and tasks. |
| Local vs global knowledge | An allocator reading peer state may read only the deciding UAV's own store, never another UAV's authoritative position. |
| Observation vs behaviour | Metrics and traces are derived from transitions. They never feed decisions back into the simulation. |

`Mission.assert_consistent()` enforces bidirectional task ownership and is
called internally after mission start and recovery. `Simulation` also calls it
at scheduler checkpoints; preserve those checks and add one after any new
compound mutation path.

`Mission.exchange_heartbeats()` creates immutable source snapshots only. Do not
reintroduce a call that records them directly in `FailureManager`; receiver
heartbeat evidence must come from successful `PeerStateTransport.deliver()`
calls.

`CommunicationGraph` is allowed to use authoritative positions because it is
the world-level network model. The only topology fact copied into a peer store
is the pair-local delivery result exposed by
`PeerStateTransport.synchronize_link_evidence()`; do not copy positions,
components, or physical health through that adapter.

Failure suspicion requires a previously delivered observation and a direct link
that has remained continuously reachable beyond the strict timeout. Preserve
the threshold `max(2, floor((N - 1) / 2) + 1)`: the suspected UAV is excluded
from `N - 1`, and one observer must never be able to declare a peer failed.
Repeated votes make unchanged evidence retryable, while votes older than the
timeout must not count toward a declaration. Locally originated declaration
certificates remain available for graph-mediated retries, but their world-state
consequence is applied only once.

World truth may decide whether a UAV's software can execute, but inactive
software must not mutate its private freshness or link-evidence state. That
execution gate must not reveal physical health to any other replica.

Task-claim epochs are scoped to one `(task_id, owner_agent_id)` publication
stream and must never be compared across owners. A local store advances only
its own stream by exactly one on creation or renewal; a larger remote-owner
epoch cannot buy cross-owner priority. Reconciliation first supersedes older
publications per owner and then selects the lowest validated owner ID, using no
`Mission`, live remote `Agent`, or authoritative `Task` field.

Keep claim receipt and reconciliation as separate observable boundaries. A
losing owner must stop acting from its own reconciliation result and publish an
exact-generation release tombstone through `TaskClaimTransport`; do not erase
every receiver's losing evidence from a central loop. Completion is receiver
local and absorbing once valid evidence is accepted.

For the complete rationale and state vocabulary, read
[`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md)
before changing failure detection, peer state, messaging, task ownership, or
allocation.

## Adding an allocation policy

Implement the `AllocationPolicy` protocol in `task_allocator.py`:

```python
def allocate(
    self, agents: Iterable[Agent], tasks: Iterable[Task]
) -> list[Allocation]: ...
```

Return unique, non-conflicting proposals and mutate nothing. Add the policy
name to `SimulationConfig.allocation_policy` validation and to the CLI, and
cover it with a deterministic comparison test against an existing policy on
identical inputs.

The current `AllocationPolicy` interface is a centralized reference seam, not a
distributed claims protocol. If a policy contains an agent-local scorer, pass
that scorer exactly one UAV's self state and its own `PeerStateStore`; do not
give it another UAV's live `Agent`, global position, or authoritative task
ownership as peer evidence.

## Tests

New behaviour needs a test that would fail without it. Prefer exact assertions
on timestamps and counts over loose ones — this is a deterministic simulator,
so `assert duration == 17.25` is both legal and more useful than a tolerance.

Name tests after the property they protect, not the function they call.
For distributed-state changes, include a negative assertion: prove that the
forbidden shortcut—undelivered heartbeat refresh, silence-to-failure promotion,
cross-partition delivery, or live-peer lookup—cannot occur.

## Commits

Conventional-style prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
`chore:`) matching the existing history. One logical change per commit.

## Scope

This is a research prototype for a specific challenge. Contributions that add
real autopilot, flight-control, targeting, or vehicle-integration code are out
of scope and will be declined.
