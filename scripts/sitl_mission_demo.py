"""Run one coordination mission with one agent's motion driven by live SITL telemetry.

This is the integration docs/sitl.md's phased plan calls "Next": a real
``Simulation`` mission -- claims, leases, reconciliation, completion, all
exactly as usual -- where one agent's physical position comes from an
ArduPilot vehicle's own telemetry instead of the analytical point-mass model.
Every other agent still moves through the default backend, unchanged.

This is not a measured result in the sense the rest of the project uses the
term: SITL is not reproducible, so this script's output belongs in
docs/sitl.md's "Honest assessment", never in docs/measured_results.md.

Usage:
    ./scripts/sitl-copter.sh                                   # in WSL
    python scripts/sitl_mission_demo.py tcp:<wsl-ip>:5760       # on this machine
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from eudis_swarm.config import SimulationConfig
from eudis_swarm.mavlink_backend import MavlinkBackend
from eudis_swarm.simulation import Simulation
from eudis_swarm.sitl import SitlVehicle

#: The integrated agent's id. It must exist in the configured scenario.
INTEGRATED_AGENT_ID = 1

#: A real multirotor settles 1.4-2.0 m from a commanded point (docs/sitl.md,
#: phase 3). The simulator's default 0.75 m arrival tolerance assumes a point
#: mass and would never be met, so a live mission uses the measured envelope.
LIVE_COMPLETION_TOLERANCE_M = 3.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("connection", help="pymavlink connection string")
    parser.add_argument("--altitude", type=float, default=20.0)
    parser.add_argument(
        "--step-timeout",
        type=float,
        default=30.0,
        help="wall-clock seconds to wait for one step's worth of vehicle time",
    )
    arguments = parser.parse_args(argv)

    vehicle = SitlVehicle(arguments.connection, timeout=40.0)
    print(f"connecting to {arguments.connection} ...")
    vehicle.connect()
    vehicle.arm_and_takeoff(arguments.altitude)
    print(f"airborne at {vehicle.altitude_m:.1f} m; starting the mission")

    config = replace(
        SimulationConfig(),
        agent_count=2,
        task_count=2,
        task_control="distributed",
        max_simulation_time=120.0,
        random_seed=7,
        completion_tolerance=LIVE_COMPLETION_TOLERANCE_M,
    )
    backend = MavlinkBackend(
        {INTEGRATED_AGENT_ID: vehicle}, step_timeout=arguments.step_timeout
    )

    try:
        result = Simulation(config, vehicle_backend=backend).run()
    finally:
        vehicle.close()

    print(f"completed {result.metrics.completed_task_count}/{config.task_count} tasks")
    print(
        f"mission duration (logical seconds): {result.metrics.simulation_duration:.2f}"
    )
    print(
        f"agent {INTEGRATED_AGENT_ID} final position (from live telemetry): "
        f"{result.position_history[INTEGRATED_AGENT_ID][-1][1]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
