# Security policy

## Scope of this project

EUDIS Swarm is a **deterministic, offline simulation**. It has no network
listeners, no authentication, no persistence layer, no user accounts, and no
runtime dependencies outside the Python standard library. The optional
dashboard binds to localhost only.

There is therefore no production attack surface. The realistic security
concerns for this repository are supply-chain integrity of the development
dependencies and correctness of the simulation itself.

## Not flight software

This repository models UAV coordination algorithmically. It contains no
autopilot, guidance, navigation, control, targeting, or safety-critical code,
and no interface to any real vehicle, radio, or autopilot stack. Do not deploy
it on hardware.

## Supported versions

Only the latest commit on `main` is supported. Prototype tags are historical
snapshots and receive no fixes.

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/n0711/eudis-swarm/security/advisories/new)
rather than opening a public issue.

Please include the affected version or commit, reproduction steps, and the
impact you believe it has. Expect an acknowledgement within seven days.

## Reporting a correctness defect

A simulation that reports the wrong result is the more likely and more damaging
failure mode here. Those are **not** security issues — open a normal issue, and
include the exact command line, the observed output, and the expected output.
Every published result in the README is reproducible from a documented command,
so a mismatch is always actionable.
