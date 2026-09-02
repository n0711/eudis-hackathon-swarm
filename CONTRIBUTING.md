# Contributing

Thanks for looking at EUDIS Swarm. This document covers the working agreement
for the repository — most of it exists to protect one property: **every
published result must stay exactly reproducible**.

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
| Physical vs communication state | `IDLE`/`ACTIVE`/`FAILED` never derives from link state. `UNREACHABLE`, `STALE` and `UNKNOWN` must never imply `FAILED`. |
| Proposal vs authority | Allocators propose `Allocation` records. Only `Mission` mutates agents and tasks. |
| Local vs global knowledge | An allocator reading peer state may read only the deciding UAV's own store, never another UAV's authoritative position. |
| Observation vs behaviour | Metrics and traces are derived from transitions. They never feed decisions back into the simulation. |

`Mission.assert_consistent()` enforces bidirectional task ownership and is
called after every state-changing operation. Leave those calls in place.

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

## Tests

New behaviour needs a test that would fail without it. Prefer exact assertions
on timestamps and counts over loose ones — this is a deterministic simulator,
so `assert duration == 17.25` is both legal and more useful than a tolerance.

Name tests after the property they protect, not the function they call.

## Commits

Conventional-style prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
`chore:`) matching the existing history. One logical change per commit.

## Scope

This is a research prototype for a specific challenge. Contributions that add
real autopilot, flight-control, targeting, or vehicle-integration code are out
of scope and will be declined.
