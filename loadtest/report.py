"""Regenerates the load tests' tables and chart from the committed raw output (D-122).

    uv run python loadtest/report.py

Reads loadtest/runs.json and docs/results/raw/; writes docs/results/summary.json,
docs/results/tables.md and docs/results/m8-outage.svg. The logic is `lgwload.report`.
"""

import sys
from pathlib import Path

from lgwload.report import main

if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parent.parent))
