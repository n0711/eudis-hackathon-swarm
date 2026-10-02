"""Bundle one laboratory test run into a verifiable evidence folder.

Every row of the evidence log in docs/trl4_validation.md should point at one of
these bundles. A bundle records what was run, on which commit and machine, what
it printed, and a SHA-256 of every artefact, so a reader can check that the
files they are shown are the files the run produced.

LT1 (the deterministic baseline) is run by this script itself. Every other test
card produces its own logs; attach them here afterwards:

    python scripts/collect_evidence.py LT1
    python scripts/collect_evidence.py LT3 --result PASS \\
        --note "4x ArduCopter SITL, speedup 5, UAV 3 fail-stopped at t=20" \\
        --attach sitl-run/evidence.jsonl sitl-run/report.txt

Wall-clock time appears here only as a label on the bundle. Nothing in this
script feeds back into the simulator.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

TEST_IDS = ("LT1", "LT2", "LT3", "LT4", "LT5", "LT6", "LT7")
RESULTS = ("PASS", "FAIL", "INCONCLUSIVE")

#: LT1's commands and what the test card says each must show.
LT1_STEPS: tuple[tuple[str, list[str], tuple[str, ...], int], ...] = (
    (
        "pytest",
        [sys.executable, "-m", "pytest", "--cov=eudis_swarm", "-q"],
        ("Required test coverage of 85.0% reached",),
        0,
    ),
    (
        "demo-quorum-floor",
        [
            sys.executable,
            "-m",
            "eudis_swarm.simulation",
            "--scenario",
            "demo-quorum-floor",
        ],
        ("Tasks completed: 3 / 3", "Mission completed at t=10.75s"),
        0,
    ),
    (
        "baseline-quorum-floor",
        [
            sys.executable,
            "-m",
            "eudis_swarm.simulation",
            "--scenario",
            "baseline-quorum-floor",
        ],
        ("Tasks completed: 2 / 3",),
        1,
    ),
)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_lt1(bundle: Path) -> tuple[str, list[str]]:
    """Run LT1, write each step's output, and judge it against the test card."""

    failures: list[str] = []
    for name, command, must_contain, expected_exit in LT1_STEPS:
        completed = subprocess.run(command, capture_output=True, text=True)
        output = completed.stdout + completed.stderr
        (bundle / f"{name}.txt").write_text(output, encoding="utf-8")
        if completed.returncode != expected_exit:
            failures.append(
                f"{name}: exit {completed.returncode}, expected {expected_exit}"
            )
        for needle in must_contain:
            if needle not in output:
                failures.append(f"{name}: missing {needle!r}")
    return ("PASS" if not failures else "FAIL"), failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test_id", choices=TEST_IDS)
    parser.add_argument("--attach", nargs="*", default=[], type=Path)
    parser.add_argument("--result", choices=RESULTS)
    parser.add_argument("--note", default="")
    parser.add_argument("--out", type=Path, default=Path("evidence"))
    arguments = parser.parse_args(argv)

    if arguments.test_id != "LT1" and arguments.result is None:
        parser.error(f"{arguments.test_id} needs --result: this script cannot judge it")

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = arguments.out / f"{stamp}-{arguments.test_id}"
    bundle.mkdir(parents=True, exist_ok=False)

    failures: list[str] = []
    if arguments.test_id == "LT1":
        result, failures = run_lt1(bundle)
        if arguments.result is not None and arguments.result != result:
            failures.append(f"--result {arguments.result} overridden by checks")
    else:
        result = arguments.result

    for source in arguments.attach:
        if not source.is_file():
            parser.error(f"attachment not found: {source}")
        shutil.copy2(source, bundle / source.name)

    artefacts = {
        path.name: _sha256(path) for path in sorted(bundle.iterdir()) if path.is_file()
    }
    manifest = {
        "test_id": arguments.test_id,
        "result": result,
        "failures": failures,
        "note": arguments.note,
        "recorded_at_utc": stamp,
        "commit": _git("rev-parse", "HEAD"),
        "working_tree_clean": _git("status", "--porcelain", "--untracked-files=no")
        == "",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "host": platform.node(),
        "artefacts_sha256": artefacts,
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"{arguments.test_id}: {result} -> {bundle}")
    for failure in failures:
        print(f"  {failure}")
    if not manifest["working_tree_clean"]:
        print("  warning: uncommitted changes; this run is not tied to a clean commit")
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
