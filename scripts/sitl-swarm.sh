#!/usr/bin/env bash
# Start (or stop) N ArduPilot SITL instances for the multi-vehicle mission.
#
#   scripts/sitl-swarm.sh N [SPEEDUP]          N instances (default speedup 5)
#   scripts/sitl-swarm.sh TYPE... [SPEEDUP]    one instance per TYPE, in order:
#                                              copter or rover
#   scripts/sitl-swarm.sh stop                 stop every instance started here
#   scripts/sitl-swarm.sh status               list running instances
#
# Instance i (0-based) is agent i+1. It serves MAVLink on TCP 5760+10*i,
# runs in its own directory under RUN_DIR (own eeprom.bin and logs), uses
# MAVLink system id i+1, and starts at the geodetic point matching agent
# i+1's simulated start position relative to ORIGIN -- so the vehicles never
# overlap and one shared local frame describes all of them. Connect with:
#
#   python scripts/sitl_swarm_mission.py --count N --host <host> \
#       [--vehicles copter,copter,rover]
#
# A copter is ArduCopter (binary arducopter, model FRAME, copter.parm); a
# rover is ArduRover (binary ardurover, model ROVER_FRAME, rover.parm). The
# names sim_vehicle.py uses (-v ArduCopter / -v Rover) do not lower-case to
# the binary names, so the mapping is explicit here.
#
# Environment:
#   ARDUPILOT_DIR  ArduPilot checkout with the binaries built (default ~/ardupilot)
#   VEHICLES       comma-separated types for the "N" form, e.g.
#                  copter,copter,copter,rover (default: all copter)
#   ORIGIN         LAT,LON,ALT,HEADING of the simulator's (0,0)
#                  (default 35.1264,33.4299,100,0; must match --origin)
#   AREA           simulated area side in metres (default 100, as SimulationConfig)
#   RUN_DIR        where instance directories and the pid file live
#                  (default /tmp/eudis-sitl-swarm)
#   FRAME          ArduCopter frame model (default quad)
#   ROVER_FRAME    ArduRover frame model (default rover)
#   COPTER_PARAMS  extra .parm file loaded into every copter after the stock
#                  defaults, e.g. config/sitl/copter-tuned.parm (SITL only)
#   DRY_RUN=1      print what would be started and exit; needs no ArduPilot
#
# Prerequisites: Linux or WSL2 with an ArduPilot checkout and
#   cd "$ARDUPILOT_DIR" && ./waf configure --board sitl && ./waf copter
#   (and ./waf rover for any rover)
# Each instance waits on its TCP port until a client connects, so starting
# them costs nothing until the mission script attaches.
set -Eeuo pipefail

ARDUPILOT_DIR="${ARDUPILOT_DIR:-$HOME/ardupilot}"
ORIGIN="${ORIGIN:-35.1264,33.4299,100,0}"
AREA="${AREA:-100}"
RUN_DIR="${RUN_DIR:-/tmp/eudis-sitl-swarm}"
FRAME="${FRAME:-quad}"
ROVER_FRAME="${ROVER_FRAME:-rover}"
VEHICLES="${VEHICLES:-}"
COPTER_PARAMS="${COPTER_PARAMS:-}"
DRY_RUN="${DRY_RUN:-0}"
BASE_PORT=5760
PORT_STRIDE=10
MAX_INSTANCES=16
PID_FILE="${RUN_DIR}/pids"

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

stop_all() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "no instances recorded in ${PID_FILE}"
        return 0
    fi
    while read -r instance pid; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            echo "stopped instance ${instance} (pid ${pid})"
        fi
    done <"$PID_FILE"
    sleep 1
    while read -r _instance pid; do
        kill -9 "$pid" 2>/dev/null || true
    done <"$PID_FILE"
    rm -f "$PID_FILE"
}

status_all() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "no instances recorded in ${PID_FILE}"
        return 0
    fi
    while read -r instance pid; do
        local state="stopped"
        kill -0 "$pid" 2>/dev/null && state="running"
        echo "instance ${instance}: pid ${pid} port $((BASE_PORT + PORT_STRIDE * instance)) ${state}"
    done <"$PID_FILE"
}

# Print "instance lat lon alt heading" per vehicle. Start positions follow
# eudis_swarm.simulation._initial_positions: the first four in the area
# corners at a 5% margin, any further ones on a ring of radius 0.35*AREA.
# The equirectangular projection matches eudis_swarm.geodesy.LocalFrame;
# tests/test_sitl_swarm.py checks both against the package.
homes() {
    python3 - "$1" "$ORIGIN" "$AREA" <<'PY'
import math, sys
count = int(sys.argv[1])
parts = sys.argv[2].split(",")
if len(parts) != 4:
    sys.exit("ORIGIN must be LAT,LON,ALT,HEADING")
lat0, lon0, alt, heading = (float(p) for p in parts)
area = float(sys.argv[3])
margin = area * 0.05
corners = [(margin, margin), (area - margin, margin), (margin, area - margin),
           (area - margin, area - margin)]
positions = corners[:count]
extra = count - len(corners)
for index in range(max(0, extra)):
    angle = 2.0 * math.pi * index / extra
    positions.append((area / 2 + area * 0.35 * math.cos(angle),
                      area / 2 + area * 0.35 * math.sin(angle)))
radius = 6378137.0
per_lat = math.radians(1.0) * radius
per_lon = per_lat * math.cos(math.radians(lat0))
for index, (east, north) in enumerate(positions):
    print(f"{index} {lat0 + north / per_lat:.8f} {lon0 + east / per_lon:.8f} "
          f"{alt:g} {heading:g}")
PY
}

# binary, model and defaults file for one vehicle type
vehicle_binary() { echo "${ARDUPILOT_DIR}/build/sitl/bin/ardu$1"; }
vehicle_model() { if [[ "$1" == "rover" ]]; then echo "$ROVER_FRAME"; else echo "$FRAME"; fi; }
vehicle_defaults() {
    local stock="${ARDUPILOT_DIR}/Tools/autotest/default_params/$1.parm"
    if [[ "$1" == "copter" && -n "$COPTER_PARAMS" ]]; then
        echo "${stock},${COPTER_PARAMS}"
    else
        echo "$stock"
    fi
}

start_all() {
    local count="$1" speedup="$2"
    shift 2
    local types=("$@")
    if ! [[ "$count" =~ ^[0-9]+$ ]] || (( count < 1 || count > MAX_INSTANCES )); then
        echo "ERROR: N must be an integer from 1 to ${MAX_INSTANCES}, got '${count}'" >&2
        exit 2
    fi
    if ! [[ "$speedup" =~ ^[0-9]+$ ]] || (( speedup < 1 )); then
        echo "ERROR: SPEEDUP must be a positive integer, got '${speedup}'" >&2
        exit 2
    fi
    if (( ${#types[@]} == 1 )); then
        local single="${types[0]}"
        types=()
        for (( i = 0; i < count; i++ )); do types+=("$single"); done
    fi
    if (( ${#types[@]} != count )); then
        echo "ERROR: ${#types[@]} vehicle types given for ${count} instances" >&2
        exit 2
    fi
    local kind
    for kind in "${types[@]}"; do
        if [[ "$kind" != "copter" && "$kind" != "rover" ]]; then
            echo "ERROR: vehicle type must be copter or rover, got '${kind}'" >&2
            exit 2
        fi
    done

    if [[ -n "$COPTER_PARAMS" ]]; then
        if [[ ! -f "$COPTER_PARAMS" ]]; then
            echo "ERROR: COPTER_PARAMS file '${COPTER_PARAMS}' does not exist" >&2
            exit 2
        fi
        COPTER_PARAMS="$(cd "$(dirname "$COPTER_PARAMS")" && pwd)/$(basename "$COPTER_PARAMS")"
    fi
    if [[ "$DRY_RUN" != "1" ]]; then
        for kind in $(printf '%s\n' "${types[@]}" | sort -u); do
            if [[ ! -x "$(vehicle_binary "$kind")" ]]; then
                echo "ERROR: $(vehicle_binary "$kind") is not built. Build it with:" >&2
                echo "  cd ${ARDUPILOT_DIR} && ./waf configure --board sitl && ./waf ${kind}" >&2
                exit 1
            fi
        done
        if [[ -f "$PID_FILE" ]]; then
            echo "ERROR: instances already recorded in ${PID_FILE}; run '$0 stop' first" >&2
            exit 1
        fi
        mkdir -p "$RUN_DIR"
    fi

    local home_lines
    home_lines="$(homes "$count")"
    while read -r instance lat lon alt heading; do
        local dir="${RUN_DIR}/inst${instance}"
        local port=$((BASE_PORT + PORT_STRIDE * instance))
        local home="${lat},${lon},${alt},${heading}"
        kind="${types[$instance]}"
        local command=("$(vehicle_binary "$kind")" --model "$(vehicle_model "$kind")"
            --speedup "$speedup" -I "$instance" --sysid "$((instance + 1))"
            --home "$home" --defaults "$(vehicle_defaults "$kind")")
        if [[ "$DRY_RUN" == "1" ]]; then
            echo "instance ${instance} agent $((instance + 1)) port ${port} home ${home} type ${kind}"
            echo "  (cd ${dir} && ${command[*]})"
            continue
        fi
        mkdir -p "$dir"
        # The SITL binary exits as soon as it notices its parent process has
        # gone, so it must not be exec'd from this short-lived script: a
        # small waiting subshell stays behind as its parent. That subshell
        # records the binary's own pid (what 'stop' kills) and its exit
        # status. stdin is /dev/null so no instance reads this loop's input.
        (
            cd "$dir"
            "${command[@]}" </dev/null >"${dir}/sitl.log" 2>&1 &
            echo "${instance} $!" >>"$PID_FILE"
            wait $! && status=0 || status=$?
            echo "[sitl-swarm] instance ${instance} exited with status ${status}" >>"${dir}/sitl.log"
        ) </dev/null >/dev/null 2>&1 &
        echo "started instance ${instance} (agent $((instance + 1)), ${kind}) on TCP ${port}, home ${home}"
    done <<<"$home_lines"
    # Every subshell appends its pid before the binary does any work; give
    # them a moment so 'status' straight after start is complete.
    sleep 1
    if [[ "$DRY_RUN" != "1" ]]; then
        echo "logs: ${RUN_DIR}/inst*/sitl.log   stop with: $0 stop"
    fi
}

[[ $# -ge 1 ]] || usage
case "$1" in
    stop) stop_all ;;
    status) status_all ;;
    -h|--help) usage ;;
    copter|rover)
        # TYPE... [SPEEDUP]: every leading word is a vehicle type.
        types=()
        while [[ $# -gt 0 && ( "$1" == "copter" || "$1" == "rover" ) ]]; do
            types+=("$1")
            shift
        done
        [[ $# -le 1 ]] || usage
        start_all "${#types[@]}" "${1:-5}" "${types[@]}"
        ;;
    *)
        [[ $# -le 2 ]] || usage
        IFS=',' read -r -a env_types <<<"${VEHICLES:-copter}"
        start_all "$1" "${2:-5}" "${env_types[@]}"
        ;;
esac
