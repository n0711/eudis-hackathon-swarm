"""Fly one coordination mission on N ArduPilot SITL vehicles, then compare.

Starts nothing itself: launch the vehicles first with scripts/sitl-swarm.sh.
Everything this prints for the live run is NON-deterministic and belongs in
docs/sitl.md, never in docs/measured_results.md. See docs/sitl.md, Phase 5,
for the full lab procedure.

Usage:
    ./scripts/sitl-swarm.sh 4 5                                   # in WSL
    python scripts/sitl_swarm_mission.py --count 4 --host 127.0.0.1
    python scripts/sitl_swarm_mission.py --help
"""

from __future__ import annotations

import sys

from eudis_swarm.sitl_swarm import main

if __name__ == "__main__":
    sys.exit(main())
