#!/usr/bin/env bash
# Start one ArduCopter SITL instance for the eudis-swarm vehicle backend.
#
# Run this inside WSL (or any Linux with ArduPilot built). It starts the
# autopilot headless and leaves it serving MAVLink on TCP 5760, which is what
# eudis_swarm.sitl.SitlVehicle connects to.
#
# From Windows, connect to tcp:<wsl-ip>:5760. Find the address with:
#     wsl -d Ubuntu-22.04 -- hostname -I
# It changes when WSL restarts, which is the first thing to check when a
# connection that worked yesterday times out today.
set -Eeuo pipefail

ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/ardupilot}"
VEHICLE="${VEHICLE:-ArduCopter}"
FRAME="${FRAME:-quad}"
SPEEDUP="${SPEEDUP:-10}"
# Nicosia, so local coordinates land where the aircraft this models would fly.
HOME_LOCATION="${HOME_LOCATION:-35.1264,33.4299,100,0}"

if [[ ! -d "$ARDUPILOT_DIR" ]]; then
    echo "ERROR: no ArduPilot checkout at ${ARDUPILOT_DIR}." >&2
    echo "Set ARDUPILOT_DIR, or clone https://github.com/ArduPilot/ardupilot" >&2
    exit 1
fi

BINARY="${ARDUPILOT_DIR}/build/sitl/bin/$(echo "$VEHICLE" | tr '[:upper:]' '[:lower:]')"
if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: ${VEHICLE} is not built. Build it with:" >&2
    echo "  cd ${ARDUPILOT_DIR} && ./waf configure --board sitl && ./waf ${VEHICLE#Ardu}" >&2
    exit 1
fi

echo "Starting ${VEHICLE} (${FRAME}) at ${HOME_LOCATION}, speedup ${SPEEDUP}"
echo "MAVLink will be served on TCP 5760. Stop with Ctrl-C."

exec python3 "${ARDUPILOT_DIR}/Tools/autotest/sim_vehicle.py" \
    -v "$VEHICLE" \
    -f "$FRAME" \
    --no-mavproxy \
    --no-rebuild \
    --speedup "$SPEEDUP" \
    --custom-location="$HOME_LOCATION"
