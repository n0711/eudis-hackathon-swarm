<!-- Keep this short. The checklist matters more than the prose. -->

## What this changes

## Why

## Effect on published results

<!--
Every number in the README is regression-tested. State one of:
  - "None." (preferred)
  - The figures that move, why the change is deliberate, and where you updated
    the README and the tests in this same PR.
-->

## Checklist

- [ ] `python -m ruff check .` and `python -m ruff format --check .` pass
- [ ] `python -m pyright` reports zero errors
- [ ] `python -m pytest --cov=eudis_swarm --cov-report=term-missing` passes above the 85% gate
- [ ] New behaviour has a test that fails without the change
- [ ] No wall-clock time, unseeded randomness, or order-dependent iteration reaching an output
- [ ] Architectural boundaries in `CONTRIBUTING.md` are preserved
- [ ] `CHANGELOG.md` updated under `Unreleased`
