"""Measure how close ArduRover SITL settles to commanded points (GUIDED)."""

from __future__ import annotations

import sys
import time

from eudis_swarm.sitl import SitlVehicle, _require_mavutil


def arm_guided(vehicle: SitlVehicle) -> None:
    connection = vehicle._require_connection()
    mavutil = _require_mavutil()
    connection.set_mode("GUIDED")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        connection.mav.command_long_send(
            connection.target_system,
            1,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        ack = connection.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
        if (
            ack is not None
            and ack.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
            and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
        ):
            return
    raise TimeoutError("rover never armed")


def settle(vehicle: SitlVehicle, target, timeout_vehicle_s: float = 120.0):
    vehicle.command_toward(target, altitude_m=0.0)
    start_t = vehicle.vehicle_time_s
    start_p = vehicle.position
    first_within_3 = None
    last_p = vehicle.position
    still_since = None
    while vehicle.vehicle_time_s - start_t < timeout_vehicle_s:
        vehicle.observe(0.05)
        p = vehicle.position
        t = vehicle.vehicle_time_s - start_t
        if first_within_3 is None and vehicle.distance_to(target) <= 3.0:
            first_within_3 = t
        moved = ((p[0] - last_p[0]) ** 2 + (p[1] - last_p[1]) ** 2) ** 0.5
        last_p = p
        if moved < 0.01:
            still_since = still_since if still_since is not None else t
            if t - still_since > 2.0 and vehicle.distance_to(target) < 10.0:
                break
        else:
            still_since = None
    leg = ((target[0] - start_p[0]) ** 2 + (target[1] - start_p[1]) ** 2) ** 0.5
    return (
        leg,
        first_within_3,
        vehicle.vehicle_time_s - start_t,
        vehicle.distance_to(target),
    )


def main() -> int:
    vehicle = SitlVehicle(sys.argv[1], timeout=60.0)
    vehicle.connect()
    arm_guided(vehicle)
    print(f"{'leg_m':>6} {'t_within3_s':>11} {'t_settled_s':>11} {'settle_err_m':>12}")
    targets = [
        (20.0, 0.0),
        (20.0, 20.0),
        (0.0, 20.0),
        (0.0, 0.0),
        (30.0, 10.0),
        (0.0, 0.0),
    ]
    for target in targets:
        leg, t3, ts, err = settle(vehicle, target)
        t3s = f"{t3:.1f}" if t3 is not None else "never"
        print(f"{leg:6.1f} {t3s:>11} {ts:11.1f} {err:12.2f}")
    vehicle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
