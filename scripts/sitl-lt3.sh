#!/usr/bin/env bash
# Run the LT3 test card end to end: fresh SITL instances, fly, stop, verdict.
#
#   scripts/sitl-lt3.sh [RUNS] [SPEEDUP] [-- MISSION_ARGS...]
#
# For every run (default 3): start fresh instances with sitl-swarm.sh, fly
# sitl_swarm_mission.py, stop the instances. Then summarise every log with
# sitl_swarm_report.py --check, which evaluates the test card's P1-P8 and
# decides the exit status: 0 when every run passed, 1 otherwise. Everything
# lands in OUT_DIR: runN.jsonl (evidence log), runN.txt (console), report.txt.
#
#   scripts/sitl-lt3.sh                       # 3 runs, 4 copters, speedup 5
#   VEHICLES=copter,copter,copter,rover scripts/sitl-lt3.sh 3 5 -- \
#       --failure-agent 4 --failure-time 8
#
# Environment:
#   OUT_DIR    where the results go (default lt3-<UTC timestamp>)
#   COUNT      number of vehicles (default: from VEHICLES, else 4)
#   VEHICLES   comma-separated vehicle types, passed to both the launcher and
#              the mission (default: all copter)
#   PYTHON     interpreter with this package installed (default python3)
#   DRY_RUN=1  print the plan and exit; starts nothing
#   plus everything sitl-swarm.sh reads (ARDUPILOT_DIR, ORIGIN, RUN_DIR ...)
#
# Then bundle the evidence:
#   python scripts/collect_evidence.py LT3 --result PASS|FAIL --note "..." \
#       --attach OUT_DIR/run*.jsonl OUT_DIR/run*.txt OUT_DIR/report.txt
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
DRY_RUN="${DRY_RUN:-0}"
VEHICLES="${VEHICLES:-}"
RUNS=3
SPEEDUP=5
MISSION_ARGS=()

usage() {
    sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

positional=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --) shift; MISSION_ARGS=("$@"); break ;;
        -h|--help) usage ;;
        *) positional+=("$1"); shift ;;
    esac
done
(( ${#positional[@]} <= 2 )) || usage
RUNS="${positional[0]:-$RUNS}"
SPEEDUP="${positional[1]:-$SPEEDUP}"
if ! [[ "$RUNS" =~ ^[0-9]+$ ]] || (( RUNS < 1 )); then
    echo "ERROR: RUNS must be a positive integer, got '${RUNS}'" >&2
    exit 2
fi
if ! [[ "$SPEEDUP" =~ ^[0-9]+$ ]] || (( SPEEDUP < 1 )); then
    echo "ERROR: SPEEDUP must be a positive integer, got '${SPEEDUP}'" >&2
    exit 2
fi

if [[ -n "$VEHICLES" ]]; then
    IFS=',' read -r -a types <<<"$VEHICLES"
    COUNT="${COUNT:-${#types[@]}}"
else
    COUNT="${COUNT:-4}"
fi
if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || (( COUNT < 1 )); then
    echo "ERROR: COUNT must be a positive integer, got '${COUNT}'" >&2
    exit 2
fi

OUT_DIR="${OUT_DIR:-lt3-$(date -u +%Y%m%dT%H%M%SZ)}"
# One instance directory per run, never reused: every run starts fresh.
SWARM_RUN_ROOT="${RUN_DIR:-/tmp/eudis-sitl-lt3}"

mission=("$PYTHON" "${HERE}/sitl_swarm_mission.py" --count "$COUNT")
[[ -n "$VEHICLES" ]] && mission+=(--vehicles "$VEHICLES")
mission+=("${MISSION_ARGS[@]}")

echo "LT3: ${RUNS} run(s), ${COUNT} vehicle(s) (${VEHICLES:-all copter}), speedup ${SPEEDUP}"
echo "results: ${OUT_DIR}"
if [[ "$DRY_RUN" == "1" ]]; then
    for (( run = 1; run <= RUNS; run++ )); do
        echo "run ${run}: RUN_DIR=${SWARM_RUN_ROOT}/run${run} sitl-swarm.sh ${COUNT} ${SPEEDUP}"
        echo "  ${mission[*]} --log ${OUT_DIR}/run${run}.jsonl"
    done
    echo "then: ${PYTHON} ${HERE}/sitl_swarm_report.py --check ${OUT_DIR}/run*.jsonl"
    exit 0
fi

mkdir -p "$OUT_DIR"
logs=()
for (( run = 1; run <= RUNS; run++ )); do
    export RUN_DIR="${SWARM_RUN_ROOT}/run${run}"
    rm -rf "$RUN_DIR"
    echo
    echo "=== run ${run}/${RUNS} ==="
    VEHICLES="${VEHICLES:-copter}" bash "${HERE}/sitl-swarm.sh" "$COUNT" "$SPEEDUP"
    status=0
    "${mission[@]}" --log "${OUT_DIR}/run${run}.jsonl" 2>&1 \
        | grep --line-buffered -v "EOF on TCP socket" \
        | tee "${OUT_DIR}/run${run}.txt" || status=$?
    head -n 5 "${RUN_DIR}/inst0/sitl.log" >"${OUT_DIR}/run${run}-sitl-head.txt" 2>/dev/null || true
    bash "${HERE}/sitl-swarm.sh" stop >/dev/null
    if [[ -f "${OUT_DIR}/run${run}.jsonl" ]]; then
        logs+=("${OUT_DIR}/run${run}.jsonl")
    else
        echo "run ${run}: the mission wrote no evidence log (status ${status}); counted as FAIL"
        echo "run ${run}: FAIL, no evidence log (mission status ${status})" >>"${OUT_DIR}/failed-runs.txt"
    fi
done

echo
echo "=== verdict ==="
verdict=0
if (( ${#logs[@]} > 0 )); then
    "$PYTHON" "${HERE}/sitl_swarm_report.py" --check "${logs[@]}" >"${OUT_DIR}/report.txt" || verdict=$?
    tail -n 1 "${OUT_DIR}/report.txt"
else
    verdict=1
fi
if [[ -f "${OUT_DIR}/failed-runs.txt" ]]; then
    cat "${OUT_DIR}/failed-runs.txt"
    verdict=1
fi
echo "full report: ${OUT_DIR}/report.txt"
if (( verdict == 0 )); then
    echo "LT3 PASS over ${RUNS} run(s)"
else
    echo "LT3 FAIL (see ${OUT_DIR}/report.txt)"
fi
exit "$verdict"
