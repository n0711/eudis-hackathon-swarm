"""Summarise sitl_swarm_mission.py evidence logs into a comparison table.

Needs no vehicle and no ``sitl`` extra. Paste the output into docs/sitl.md
(never docs/measured_results.md: a live run is not reproducible).

Usage:
    python scripts/sitl_swarm_report.py sitl_swarm_evidence.jsonl
"""

from __future__ import annotations

import sys

from eudis_swarm.live_report import main

if __name__ == "__main__":
    sys.exit(main())
