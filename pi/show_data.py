#!/usr/bin/env python3
"""Show what the collector has saved.

    python3 show_data.py          # last 10 readings + 24 h summary
    python3 show_data.py -n 50    # last 50 readings
"""

import argparse
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "moisture.db")


def main():
    parser = argparse.ArgumentParser(description="Show logged soil readings")
    parser.add_argument("-n", type=int, default=10, help="rows to show")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"No database yet at {DB_PATH}. Is the collector running?")
        return

    db = sqlite3.connect(DB_PATH)
    total, = db.execute("SELECT COUNT(*) FROM readings").fetchone()
    print(f"{total} readings in {DB_PATH}\n")

    rows = db.execute(
        "SELECT time, volts, percent FROM readings ORDER BY ts DESC LIMIT ?",
        (args.n,)).fetchall()
    print(f"{'time':<20} {'volts':>7} {'moisture':>9}")
    for t, v, p in reversed(rows):
        print(f"{t:<20} {v:>7.3f} {'n/a' if p is None else f'{p:.1f} %':>9}")

    since = int(time.time()) - 24 * 3600
    count, lo, hi, avg = db.execute(
        "SELECT COUNT(percent), MIN(percent), MAX(percent), AVG(percent) "
        "FROM readings WHERE ts >= ?", (since,)).fetchone()
    if count:
        print(f"\nLast 24 h: {count} readings, min {lo:.1f} %, "
              f"max {hi:.1f} %, avg {avg:.1f} %")
    db.close()


if __name__ == "__main__":
    main()
