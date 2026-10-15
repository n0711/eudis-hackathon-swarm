"""Tune ArduCopter's horizontal position control on live SITL instances.

Start copters first (``scripts/sitl-swarm.sh 4 5``); each becomes one worker.
For every candidate parameter set a worker sets the parameters (each one
confirmed by the autopilot's own PARAM_VALUE echo), then flies a fixed test
pattern relative to its home:

* four GUIDED step legs around a square (``--leg`` metres each), each held
  until settled, measuring arrival time, overshoot, cross-track error,
  settle error, hold oscillation and altitude excursion;
* one long leg cut by BRAKE as it passes ``--brake-speed``, measuring the
  stopping distance and time and any rebound.

``eudis_swarm.sitl_tuning`` scores each trial (stability limits disqualify
outright) and drives a staged coordinate search: PIDs first, then motion
shaping, for ``--rounds`` rounds. The winner and the defaults are then flown
``--confirm`` more times each; the result is written as a ``.parm`` file the
launcher can load (``PARAMS=... scripts/sitl-swarm.sh``) and every trial goes
to a JSON-lines log.

These numbers tune ArduPilot's simulated quad. They are not a tune for any
real airframe.

Usage:
    ARDUPILOT_DIR=~/ardupilot scripts/sitl-swarm.sh 4 5
    python scripts/sitl_tune.py --workers 4 --out config/sitl/copter-tuned.parm
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict

from eudis_swarm.sitl_swarm import DEFAULT_ORIGIN
from eudis_swarm.sitl_tuning import (
    DEFAULTS,
    STAGES,
    Sample,
    TrialResult,
    best,
    brake_metrics,
    confirm,
    format_confirmation,
    format_trial,
    leg_metrics,
    neighbours,
    parm_file,
    read_parm,
    score,
    validate_params,
)


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Worker:
    """One live SITL copter that flies trials, one at a time."""

    def __init__(self, vehicle, home: tuple[float, float], arguments) -> None:  # noqa: ANN001
        self.vehicle = vehicle
        self.home = home
        self.arguments = arguments

    def _fly_to(
        self, target: tuple[float, float], *, hold_s: float, timeout_s: float = 60.0
    ) -> tuple[float, list[Sample]]:
        vehicle = self.vehicle
        vehicle.observe(0.0)
        command_t = vehicle.vehicle_time_s
        vehicle.command_toward(target)
        samples: list[Sample] = []
        arrived_at: float | None = None
        while True:
            samples.extend(vehicle.record(0.5))
            t = samples[-1][0]
            if arrived_at is None and _distance(samples[-1][1:3], target) <= 1.0:
                arrived_at = t
            if arrived_at is not None and t - arrived_at >= hold_s:
                return command_t, samples
            if t - command_t > timeout_s:
                return command_t, samples

    def trial(self, params: dict[str, float]) -> TrialResult:
        vehicle = self.vehicle
        try:
            for name, value in sorted(params.items()):
                vehicle.set_param(name, value)
            # GUIDED reads WP_ACC/WP_JERK only when it initialises, and asking
            # for GUIDED while already in GUIDED changes nothing: pass through
            # BRAKE so every candidate flies with its own limits, not the last
            # candidate's.
            if not vehicle.set_mode("BRAKE") or not vehicle.set_mode("GUIDED"):
                raise RuntimeError("BRAKE -> GUIDED re-initialisation not confirmed")
            self._fly_to(self.home, hold_s=2.0)
            leg = self.arguments.leg
            hx, hy = self.home
            corners = [(hx + leg, hy), (hx + leg, hy + leg), (hx, hy + leg), (hx, hy)]
            legs = []
            start = self.home
            for corner in corners:
                command_t, samples = self._fly_to(corner, hold_s=self.arguments.hold)
                legs.append(
                    leg_metrics(
                        samples,
                        start,
                        corner,
                        command_t=command_t,
                        hold_s=self.arguments.hold,
                    )
                )
                start = corner
            brake = self._brake_test()
            vehicle.set_mode("GUIDED")
            self._fly_to(self.home, hold_s=1.0)
            return TrialResult(dict(params), tuple(legs), brake)
        except (TimeoutError, RuntimeError, ValueError) as error:
            try:
                vehicle.set_mode("GUIDED")
                self._fly_to(self.home, hold_s=1.0)
            except (TimeoutError, RuntimeError):
                pass
            return TrialResult(dict(params), (), None, error=str(error))

    def _brake_test(self):  # noqa: ANN202
        vehicle = self.vehicle
        hx, hy = self.home
        far = (hx + 400.0, hy)
        vehicle.observe(0.0)
        start_t = vehicle.vehicle_time_s
        vehicle.command_toward(far)
        samples: list[Sample] = []
        command_t: float | None = None
        while True:
            samples.extend(vehicle.record(0.2))
            t, _x, _y, _z, ve, vn = samples[-1]
            if command_t is None and math.hypot(ve, vn) >= self.arguments.brake_speed:
                vehicle._connection.set_mode("BRAKE")  # noqa: SLF001 - no wait: keep sampling
                command_t = t
            if command_t is not None and t - command_t >= self.arguments.brake_window:
                break
            if command_t is None and t - start_t > 60.0:
                raise RuntimeError(
                    f"never reached {self.arguments.brake_speed} m/s for the brake test"
                )
        if vehicle.flight_mode != "BRAKE":
            raise RuntimeError("BRAKE was not confirmed by the autopilot")
        return brake_metrics(samples, command_t=command_t)


def run_trials(
    workers: Sequence[Worker],
    candidates: Sequence[dict[str, float]],
    log: Callable[[TrialResult, str], None],
    stage: str,
) -> list[TrialResult]:
    """Fly every candidate on whichever worker is free; keep input order."""

    jobs: queue.Queue[tuple[int, dict[str, float]]] = queue.Queue()
    for index, candidate in enumerate(candidates):
        jobs.put((index, candidate))
    results: list[TrialResult | None] = [None] * len(candidates)
    lock = threading.Lock()

    def work(worker: Worker) -> None:
        while True:
            try:
                index, candidate = jobs.get_nowait()
            except queue.Empty:
                return
            result = worker.trial(candidate)
            if result.error is not None:
                # A harness hiccup (lost echo, slow link) is not a verdict on
                # the parameters: fly it once more before recording it.
                result = worker.trial(candidate)
            with lock:
                results[index] = result
                log(result, stage)

    threads = [threading.Thread(target=work, args=(w,)) for w in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [r for r in results if r is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tune ArduCopter SITL position control."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=5760)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--origin", default=f"{DEFAULT_ORIGIN[0]},{DEFAULT_ORIGIN[1]}")
    parser.add_argument("--altitude", type=float, default=20.0)
    parser.add_argument("--leg", type=float, default=40.0)
    parser.add_argument("--hold", type=float, default=7.0)
    parser.add_argument("--brake-speed", type=float, default=8.0)
    parser.add_argument("--brake-window", type=float, default=8.0)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--stages", default="pid,shaping")
    parser.add_argument("--confirm", type=int, default=3)
    parser.add_argument("--out", default="copter-tuned.parm")
    parser.add_argument("--log", default="tune-trials.jsonl")
    parser.add_argument(
        "--start",
        help="start the search from this .parm file instead of the defaults",
    )
    parser.add_argument(
        "--only",
        help="skip the search: confirm this .parm file against the defaults",
    )
    arguments = parser.parse_args(argv)
    stages = [s for s in arguments.stages.split(",") if s]
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        parser.error(f"unknown stage(s) {unknown}; choose from {sorted(STAGES)}")

    from eudis_swarm.sitl import SitlVehicle

    lat, lon = (float(v) for v in arguments.origin.split(",")[:2])
    workers: list[Worker] = []
    for index in range(arguments.workers):
        vehicle = SitlVehicle(
            f"tcp:{arguments.host}:{arguments.base_port + 10 * index}",
            timeout=60.0,
            origin=(lat, lon),
            stream_rate_hz=20,
        )
        vehicle.connect()
        workers.append(Worker(vehicle, vehicle.position, arguments))
        print(
            f"worker {index}: connected at {vehicle.position[0]:.1f},{vehicle.position[1]:.1f}"
        )
    threads = [
        threading.Thread(target=w.vehicle.arm_and_takeoff, args=(arguments.altitude,))
        for w in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print("all airborne")

    log_stream = open(arguments.log, "w", encoding="utf-8")  # noqa: SIM115

    def log(result: TrialResult, stage: str) -> None:
        record = {
            "stage": stage,
            "score": None if math.isinf(score(result)) else score(result),
            "summary": format_trial(result),
            **asdict(result),
        }
        log_stream.write(json.dumps(record) + "\n")
        log_stream.flush()
        print(f"  [{stage}] {format_trial(result)}", flush=True)

    started = time.monotonic()
    base = dict(DEFAULTS)
    if arguments.only:
        with open(arguments.only, encoding="utf-8") as stream:
            base.update(validate_params(read_parm(stream.read())))
        rounds = 0
    else:
        rounds = arguments.rounds
        if arguments.start:
            with open(arguments.start, encoding="utf-8") as stream:
                base.update(validate_params(read_parm(stream.read())))
        print("baseline: the starting point once per worker, to see the noise")
        baseline = run_trials(workers, [dict(base)] * len(workers), log, "baseline")
        if best(baseline) is None:
            print("error: the starting point itself was disqualified; check the setup")
            return 1
    base_score = math.inf if arguments.only else min(score(r) for r in baseline)
    for round_index in range(1, rounds + 1):
        for stage in stages:
            candidates = neighbours(base, STAGES[stage])
            print(f"round {round_index}, {stage}: {len(candidates)} candidates")
            results = run_trials(workers, candidates, log, f"r{round_index}-{stage}")
            winner = best(results)
            if winner is not None and score(winner) < base_score:
                base, base_score = dict(winner.params), score(winner)
                print(f"  -> new best {base_score:.2f}: {format_trial(winner)}")

    print(f"confirming: defaults and the winner, {arguments.confirm}x each")
    confirm_defaults = run_trials(
        workers, [dict(DEFAULTS)] * arguments.confirm, log, "confirm-defaults"
    )
    confirm_best = run_trials(
        workers, [dict(base)] * arguments.confirm, log, "confirm-best"
    )
    log_stream.close()

    defaults_summary = confirm(confirm_defaults)
    tuned_summary = confirm(confirm_best)
    print()
    print(format_confirmation("defaults", defaults_summary))
    print(format_confirmation("tuned   ", tuned_summary))
    if not tuned_summary.mean_score < defaults_summary.mean_score:
        print("the tune did not beat the defaults on confirmation; nothing written")
        return 2
    header = [
        "ArduCopter SITL position-control tune for the eudis-swarm missions.",
        "Produced by scripts/sitl_tune.py; SITL quad model ONLY -- never fly a real",
        "airframe on these values (AutoTune its rate loops, then tune these).",
        "Load with: COPTER_PARAMS=<this file> scripts/sitl-swarm.sh N SPEEDUP",
        "Confirmation flights (40 m GUIDED steps; BRAKE from about 8 m/s):",
        format_confirmation("  defaults", defaults_summary),
        format_confirmation("  tuned   ", tuned_summary),
        "",
    ]
    validate_params(base)
    with open(arguments.out, "w", encoding="utf-8") as stream:
        stream.write(parm_file(base, header))
    print(
        f"wrote {arguments.out} after {time.monotonic() - started:.0f} s; trials in {arguments.log}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
