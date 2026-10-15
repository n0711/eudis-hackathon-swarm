"""Record one 40 m GUIDED step and one BRAKE stop, stock vs tuned, for the paper.

Needs one live ArduCopter SITL instance (scripts/sitl-swarm.sh 1 5) and the
``sitl`` extra. Writes paper/data/step_<label>.csv and brake_<label>.csv.
SITL is not reproducible: re-running gives slightly different traces.

    python paper/tools/record_traces.py tcp:127.0.0.1:5760
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

from eudis_swarm.sitl import SitlVehicle
from eudis_swarm.sitl_tuning import DEFAULTS, read_parm

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "paper" / "data"


def apply(vehicle: SitlVehicle, params: dict[str, float]) -> None:
    for name, value in sorted(params.items()):
        vehicle.set_param(name, value)
    vehicle.set_mode("BRAKE")
    vehicle.set_mode("GUIDED")


def settle_at(vehicle: SitlVehicle, target: tuple[float, float]) -> None:
    vehicle.command_toward(target)
    vehicle.record(10.0)


def write(path: Path, rows: list[tuple[float, ...]], header: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main(connection: str) -> int:
    tuned = dict(DEFAULTS)
    tuned.update(read_parm((ROOT / "config/sitl/copter-tuned.parm").read_text()))
    vehicle = SitlVehicle(connection, origin=(35.1264, 33.4299), stream_rate_hz=20)
    vehicle.connect()
    vehicle.arm_and_takeoff(20.0)
    home = vehicle.position
    for label, params in (("stock", dict(DEFAULTS)), ("tuned", tuned)):
        apply(vehicle, params)
        settle_at(vehicle, home)
        vehicle.observe(0.0)
        t0 = vehicle.vehicle_time_s
        target = (home[0] + 40.0, home[1])
        vehicle.command_toward(target)
        samples = vehicle.record(14.0)
        write(
            OUT / f"step_{label}.csv",
            [
                (round(t - t0, 3), round(x - home[0], 3), round(math.hypot(ve, vn), 3))
                for t, x, _y, _z, ve, vn in samples
            ],
            ["t", "along", "speed"],
        )
        settle_at(vehicle, home)
        vehicle.observe(0.0)
        vehicle.command_toward((home[0] + 400.0, home[1]))
        samples = []
        command_t = None
        while True:
            samples.extend(vehicle.record(0.2))
            t, x, _y, _z, ve, vn = samples[-1]
            if command_t is None and math.hypot(ve, vn) >= 8.0:
                vehicle._connection.set_mode("BRAKE")  # noqa: SLF001
                command_t, command_x = t, x
            if command_t is not None and t - command_t >= 8.0:
                break
        write(
            OUT / f"brake_{label}.csv",
            [
                (
                    round(t - command_t, 3),
                    round(x - command_x, 3),
                    round(math.hypot(ve, vn), 3),
                )
                for t, x, _y, _z, ve, vn in samples
                if t >= command_t - 1.0
            ],
            ["t", "distance", "speed"],
        )
        vehicle.set_mode("GUIDED")
        print(f"{label}: recorded")
    vehicle.set_mode("LAND")
    vehicle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tcp:127.0.0.1:5760"))
