#!/usr/bin/env python3
"""Backfill data/daily_archive.json for the prior + current month through target_date.

Usage:
  python scripts/backfill_archive.py --date 2026-09-11 --skip-email
  python scripts/backfill_archive.py --date 2026-09-11 --demo --skip-email
  python main.py --backfill --skip-email
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import parse_target_date, resolve_target_date, run_backfill  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill daily revenue archive")
    parser.add_argument("--date", dest="target_date", metavar="YYYY-MM-DD")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--skip-email", action="store_true", default=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch days that already exist in the archive",
    )
    args = parser.parse_args()
    target = parse_target_date(args.target_date) or resolve_target_date()
    sys.exit(
        run_backfill(
            target_date=target,
            demo=args.demo,
            skip_email=True if args.skip_email else False,
            force=args.force,
            seed_baseline=True,
        )
    )


if __name__ == "__main__":
    main()
