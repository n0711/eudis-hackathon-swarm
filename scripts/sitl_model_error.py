"""Measure the gap between the planner's motion model and a real autopilot.

The simulator predicts motion with ``advance_point_mass``: constant speed,
instant acceleration, exact arrival. A real vehicle does none of those. This
flies a leg under ArduPilot SITL, samples where the vehicle actually is, and
compares each sample against what the model said would be there at that moment.

This is not a measured result in the sense the rest of the project uses the
term. SITL is not reproducible, so the output is a range over repeated legs
rather than a number, and it belongs in docs/sitl.md rather than in
docs/measured_results.md.

Usage:
    python scripts/sitl_model_error.py tcp:<wsl-ip>:5760 [--legs 3] [--length 60]
"""

from __future__ import annotations

import argparse
import sys
import time

from eudis_swarm.agent import advance_point_mass
from eudis_swarm.sitl import SitlVehicle

#: ArduCopter ships WPNAV_SPEED at 1000 cm/s, which is also the simulator's
#: default agent_speed. The comparison is therefore like for like by default.
DEFAULT_SPEED_MPS = 10.0


def _wpnav_speed_mps(vehicle: SitlVehicle, fallback: float) -> float:
    """Ask the autopilot for its own horizontal speed, so the model gets its best case.

    A parameter reply has to arrive through the same stream as everything else,
    and it is not worth failing the experiment over: if it does not come, say so
    and use the documented default rather than pretending it was read.
    """

    connection = vehicle._require_connection()  # noqa: SLF001 - experiment script
    for _ in range(3):
        connection.mav.param_request_read_send(
            connection.target_system, 1, b"WPNAV_SPEED", -1
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            message = connection.recv_match(
                type="PARAM_VALUE", blocking=True, timeout=1
            )
            if message is None:
                continue
            name = message.param_id
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if "WPNAV_SPEED" in name:
                return float(message.param_value) / 100.0
    print(f"note: WPNAV_SPEED was not returned; using {fallback:.2f} m/s")
    return fallback


def fly_leg(
    vehicle: SitlVehicle, target: tuple[float, float], speed: float, altitude: float
) -> dict[str, float]:
    """Fly one leg, sampling model error the whole way."""

    start = vehicle.position
    predicted_seconds = (
        (target[0] - start[0]) ** 2 + (target[1] - start[1]) ** 2
    ) ** 0.5 / speed

    vehicle.command_toward(target, altitude_m=altitude)
    # The vehicle's own clock, never wall-clock: SITL runs sped up, and a
    # stopwatch on this machine would report a 60 m leg taking one second.
    began = vehicle.vehicle_time_s
    wall_deadline = time.monotonic() + 120.0
    worst_error = 0.0
    worst_at = 0.0
    samples = 0
    elapsed = 0.0

    while time.monotonic() < wall_deadline:
        vehicle.observe()
        elapsed = vehicle.vehicle_time_s - began
        if elapsed > predicted_seconds * 6.0 + 20.0:
            break
        expected = advance_point_mass(start, target, speed, elapsed)
        actual = vehicle.position
        error = ((actual[0] - expected[0]) ** 2 + (actual[1] - expected[1]) ** 2) ** 0.5
        samples += 1
        if error > worst_error:
            worst_error, worst_at = error, elapsed
        if vehicle.distance_to(target) <= 2.0:
            break

    actual_seconds = elapsed
    return {
        "predicted_seconds": predicted_seconds,
        "actual_seconds": actual_seconds,
        "slower_by": actual_seconds / predicted_seconds if predicted_seconds else 0.0,
        "worst_error_m": worst_error,
        "worst_error_at_s": worst_at,
        "arrival_error_m": vehicle.distance_to(target),
        "samples": float(samples),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("connection", help="pymavlink connection string")
    parser.add_argument("--legs", type=int, default=3)
    parser.add_argument("--length", type=float, default=60.0)
    parser.add_argument("--altitude", type=float, default=20.0)
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED_MPS)
    arguments = parser.parse_args(argv)

    vehicle = SitlVehicle(arguments.connection, timeout=40.0)
    vehicle.connect()
    vehicle.arm_and_takeoff(arguments.altitude)
    speed = _wpnav_speed_mps(vehicle, arguments.speed)
    print(f"autopilot WPNAV_SPEED = {speed:.2f} m/s (used as the model's speed)")
    print(
        f"{'leg':>4} {'model s':>8} {'actual s':>9} {'ratio':>6} "
        f"{'worst err':>10} {'at s':>6} {'arrival':>8}"
    )

    # The first commanded leg lands while the vehicle is still finishing its
    # climb and is simply ignored, so it is flown and discarded rather than
    # reported as a 27x model error.
    fly_leg(vehicle, (0.0, arguments.length), speed, arguments.altitude)
    fly_leg(vehicle, (0.0, 0.0), speed, arguments.altitude)

    results = []
    for index in range(arguments.legs):
        target = (0.0, arguments.length) if index % 2 == 0 else (0.0, 0.0)
        result = fly_leg(vehicle, target, speed, arguments.altitude)
        results.append(result)
        print(
            f"{index + 1:>4} {result['predicted_seconds']:8.2f} "
            f"{result['actual_seconds']:9.2f} {result['slower_by']:6.2f} "
            f"{result['worst_error_m']:10.2f} {result['worst_error_at_s']:6.2f} "
            f"{result['arrival_error_m']:8.2f}"
        )

    print()
    for key in ("slower_by", "worst_error_m", "arrival_error_m"):
        values = [r[key] for r in results]
        print(
            f"{key}: min {min(values):.2f}  max {max(values):.2f}  "
            f"mean {sum(values) / len(values):.2f}"
        )
    vehicle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
