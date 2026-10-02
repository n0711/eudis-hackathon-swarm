"""Render a sitl_swarm_mission.py evidence log as one offline HTML page.

Needs the ``charts`` extra (plotly) and nothing else: no vehicle, no
pymavlink. The page embeds Plotly's JavaScript, so it opens offline.

Usage:
    python scripts/sitl_swarm_chart.py lt3-run1.jsonl [--output lt3-run1.html]
"""

from __future__ import annotations

import sys

from eudis_swarm.live_chart import main

if __name__ == "__main__":
    sys.exit(main())
