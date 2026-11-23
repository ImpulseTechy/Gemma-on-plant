#!/usr/bin/env python3
"""Read the soil sensor on a schedule and save every reading to SQLite.

    python3 collector.py                         # every 5 min, forever (what the service runs)
    python3 collector.py --interval 5 --count 3  # quick test: 3 readings, 5 s apart

Database: data/moisture.db (next to this script), table `readings`.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from analyzer import moisture_status
from sensor import (I2C_BUS, SMBus, load_calibration, read_average,
                    volts_to_percent)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "moisture.db")

TRIGGER_COOLDOWN = 3600   # at most one alert-triggered AI run per hour

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,   -- Unix time (seconds), easy to do math on
    time     TEXT    NOT NULL,   -- local time, easy for humans to read
    channel  INTEGER NOT NULL,   -- ADC input (0 = A0)
    volts    REAL    NOT NULL,
    percent  REAL                -- NULL if not calibrated yet
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
"""


def open_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def save_reading(db, channel, volts, percent):
    now = time.time()
    db.execute(
        "INSERT INTO readings (ts, time, channel, volts, percent) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(now), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
         channel, round(volts, 4),
         None if percent is None else round(percent, 1)))
    db.commit()


def run_analyzer_in_background(reason):
    """Start analyzer.py without waiting, so logging keeps its rhythm."""
    print(f"{time.strftime('%H:%M:%S')}  {reason} -> starting AI analysis",
          flush=True)
    subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "analyzer.py")],
                     cwd=BASE_DIR, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description="Log soil moisture to SQLite")
    parser.add_argument("--channel", type=int, default=0, choices=range(4))
    parser.add_argument("--interval", type=float, default=300,
                        help="seconds between readings (default 300 = 5 min)")
    parser.add_argument("--count", type=int, default=0,
                        help="stop after N readings (0 = run forever)")
    args = parser.parse_args()

    cal = load_calibration()
    if cal is None:
        print("WARNING: not calibrated, saving volts only.", flush=True)

    print(f"Logging A{args.channel} every {args.interval:g} s to {DB_PATH}",
          flush=True)

    n = 0
    last_status = None        # status of the previous reading
    last_trigger = 0.0
    with closing(open_db()) as db, closing(SMBus(I2C_BUS)) as bus:
        while args.count == 0 or n < args.count:
            started = time.time()
            try:
                volts = read_average(bus, args.channel, samples=10)
                percent = volts_to_percent(volts, cal) if cal else None
                save_reading(db, args.channel, volts, percent)
                pct = "n/a" if percent is None else f"{percent:.1f} %"
                print(f"{time.strftime('%H:%M:%S')}  {volts:.3f} V  {pct}",
                      flush=True)

                # Alert trigger: soil just CROSSED into DRY or WET.
                status = moisture_status(percent)
                if (last_status is not None and status != last_status
                        and status in ("DRY", "WET")
                        and time.time() - last_trigger > TRIGGER_COOLDOWN):
                    run_analyzer_in_background(
                        f"status {last_status} -> {status}")
                    last_trigger = time.time()
                last_status = status
            except OSError as e:
                # A loose wire or I2C glitch shouldn't kill the logger.
                print(f"{time.strftime('%H:%M:%S')}  read failed: {e}",
                      flush=True)
            n += 1
            if args.count and n >= args.count:
                break
            time.sleep(max(0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
