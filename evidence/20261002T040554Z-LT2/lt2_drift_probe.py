"""Measure logical-vs-vehicle time drift of MavlinkBackend on a live SITL copter."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace

from eudis_swarm.config import SimulationConfig
from eudis_swarm.mavlink_backend import MavlinkBackend
from eudis_swarm.simulation import Simulation
from eudis_swarm.sitl import SitlVehicle


class Recording:
    def __init__(self, vehicle: SitlVehicle) -> None:
        self.vehicle = vehicle
        self.rows: list[dict[str, float]] = []
        self.anchor: float | None = None
        self.logical = 0.0

    @property
    def position(self):
        return self.vehicle.position

    @property
    def vehicle_time_s(self) -> float:
        return self.vehicle.vehicle_time_s

    def command_toward(self, target):
        if self.anchor is None:
            self.anchor = self.vehicle.vehicle_time_s
        self.vehicle.command_toward(target)

    def observe(self, duration: float = 0.0):
        return self.vehicle.observe(duration)


class Backend(MavlinkBackend):
    def __init__(self, rec: Recording) -> None:
        super().__init__({1: rec}, step_timeout=30.0)
        self.rec = rec

    def move_toward(self, agent, target, elapsed):
        before = self.rec.vehicle_time_s
        wall = time.monotonic()
        super().move_toward(agent, target, elapsed)
        if agent.agent_id == 1 and agent.responsive:
            self.rec.logical += elapsed
            # Drain everything queued so "now" is the freshest report.
            stale_view = self.rec.vehicle.vehicle_time_s
            conn = self.rec.vehicle._require_connection()
            while True:
                msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=False)
                if msg is None:
                    break
                fresh = msg.time_boot_ms / 1000.0
            else:  # pragma: no cover
                pass
            try:
                fresh_view = fresh  # type: ignore[name-defined]
            except NameError:
                fresh_view = stale_view
            self.rec.rows.append(
                {
                    "logical_elapsed": self.rec.logical,
                    "vehicle_elapsed_reported": stale_view - (self.rec.anchor or 0.0),
                    "vehicle_elapsed_freshest": fresh_view - (self.rec.anchor or 0.0),
                    "step_vehicle_before": before,
                    "wall_s": time.monotonic() - wall,
                }
            )


def main() -> int:
    vehicle = SitlVehicle(sys.argv[1], timeout=40.0)
    vehicle.connect()
    vehicle.arm_and_takeoff(20.0)
    rec = Recording(vehicle)
    config = replace(
        SimulationConfig(),
        agent_count=2,
        task_count=2,
        task_control="distributed",
        max_simulation_time=120.0,
        random_seed=7,
        completion_tolerance=3.0,
    )
    result = Simulation(config, vehicle_backend=Backend(rec)).run()
    vehicle.close()
    print(json.dumps(rec.rows[:3] + rec.rows[-3:], indent=1))
    last = rec.rows[-1]
    print(
        f"steps={len(rec.rows)} logical={last['logical_elapsed']:.2f}s "
        f"vehicle(reported)={last['vehicle_elapsed_reported']:.2f}s "
        f"vehicle(freshest)={last['vehicle_elapsed_freshest']:.2f}s "
        f"completed={result.metrics.completed_task_count}/{config.task_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
