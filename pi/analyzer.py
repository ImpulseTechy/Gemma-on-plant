#!/usr/bin/env python3
"""Turn logged soil readings into advice using the offline Gemma model.

Design rule: CODE DECIDES, AI EXPLAINS.
  - Python computes the numbers and the status (DRY / OK / WET) with fixed rules.
  - Gemma only gets a short summary and writes plain-English advice.
    A 1B model can get maths wrong, so it never sees raw data or makes the call.

    python3 analyzer.py            # analyse the last 24 h, ask Gemma, save result
    python3 analyzer.py --no-ai    # stats + rules only (fast, no model)
    python3 analyzer.py --show     # print the latest saved analysis
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "moisture.db")

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma3:1b"

# --- Rules (tune these for your plant) -------------------------------------
DRY_BELOW = 30.0      # % : needs water
WET_ABOVE = 80.0      # % : too wet / waterlogged
WATERED_JUMP = 15.0   # % rise between two readings = "someone watered it"
TREND_HOURS = 3       # hours of data used to measure the trend

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    time      TEXT    NOT NULL,
    status    TEXT    NOT NULL,   -- DRY / OK / WET / NO_DATA (decided by code)
    stats     TEXT    NOT NULL,   -- JSON of the numbers sent to the model
    advice    TEXT,               -- Gemma's text (NULL if model unavailable)
    ai_secs   REAL                -- how long Gemma took
);
"""


def moisture_status(percent):
    """The rule that decides. Used by the analyzer AND the collector."""
    if percent is None:
        return "NO_DATA"
    if percent < DRY_BELOW:
        return "DRY"
    if percent > WET_ABOVE:
        return "WET"
    return "OK"


def _slope_per_hour(points):
    """Least-squares slope of (ts, percent) points, in % per hour."""
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_p = sum(p for _, p in points) / n
    num = sum((t - mean_t) * (p - mean_p) for t, p in points)
    den = sum((t - mean_t) ** 2 for t, _ in points)
    return 0.0 if den == 0 else num / den * 3600


def compute_stats(db, now=None):
    now = int(now or time.time())
    rows = db.execute(
        "SELECT ts, percent FROM readings "
        "WHERE ts >= ? AND percent IS NOT NULL ORDER BY ts",
        (now - 24 * 3600,)).fetchall()
    if not rows:
        return {"readings_24h": 0, "status": "NO_DATA"}

    last_ts, current = rows[-1]
    values = [p for _, p in rows]
    stats = {
        "current_percent": round(current, 1),
        "last_reading_minutes_ago": round((now - last_ts) / 60),
        "readings_24h": len(rows),
        "min_24h": round(min(values), 1),
        "max_24h": round(max(values), 1),
        "avg_24h": round(sum(values) / len(values), 1),
        "hours_of_data": round((last_ts - rows[0][0]) / 3600, 1),
    }

    # Trend over the last few hours (needs >= 3 points spread over >= 30 min)
    recent = [(t, p) for t, p in rows if t >= last_ts - TREND_HOURS * 3600]
    if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 1800:
        slope = _slope_per_hour(recent)
        stats["trend_percent_per_hour"] = round(slope, 2)
        if slope < -0.05 and current > DRY_BELOW:
            stats["hours_until_dry"] = round((current - DRY_BELOW) / -slope, 1)
    else:
        stats["trend_percent_per_hour"] = None   # not enough data yet

    # Was it watered? Look for a big jump between consecutive readings.
    for (t1, p1), (t2, p2) in zip(rows, rows[1:]):
        if p2 - p1 >= WATERED_JUMP:
            stats["last_watered_hours_ago"] = round((now - t2) / 3600, 1)

    stats["status"] = moisture_status(current)
    return stats


def summary_lines(s):
    """The facts given to Gemma, as short bullet lines. Shared with chat."""
    status_text = {
        "DRY": f"DRY - below {DRY_BELOW:.0f} %, the plant needs water",
        "OK": f"OK - between {DRY_BELOW:.0f} and {WET_ABOVE:.0f} %",
        "WET": f"TOO WET - above {WET_ABOVE:.0f} %, risk of root rot",
    }[s["status"]]

    action_text = {
        "DRY": "YES, water the plant now",
        "OK": "NO, the plant does not need water right now",
        "WET": "NO, do not water; let the soil drain and dry out",
    }[s["status"]]

    lines = [
        f"- Current soil moisture: {s['current_percent']} %",
        f"- Status: {status_text}",
        f"- Should it be watered? {action_text}",
        f"- Last 24 hours: min {s['min_24h']} %, max {s['max_24h']} %, "
        f"average {s['avg_24h']} % ({s['readings_24h']} readings over "
        f"{s['hours_of_data']} hours)",
    ]
    trend = s.get("trend_percent_per_hour")
    if trend is None:
        lines.append("- Trend: not enough data yet")
    else:
        word = "falling" if trend < -0.05 else "rising" if trend > 0.05 else "steady"
        lines.append(f"- Trend: {word} ({trend:+.2f} % per hour)")
    if "hours_until_dry" in s:
        lines.append(f"- At this rate it will be dry in about "
                     f"{s['hours_until_dry']} hours")
    if "last_watered_hours_ago" in s:
        lines.append(f"- Last watered about {s['last_watered_hours_ago']} "
                     f"hours ago")
    return lines


def build_prompt(s):
    lines = summary_lines(s)
    return (
        "You are a friendly garden assistant. A sensor measures the soil "
        "moisture of a potted plant. Here is the summary:\n"
        + "\n".join(lines)
        + "\n\nIn 3 short sentences: explain what this means, say what to do "
        "now, and say when to check again. Only use the numbers given above. "
        "Do not use headings or bullet points."
    )


def ask_gemma(prompt, timeout=300):
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "1m",          # free the ~0.9 GB RAM soon after
        "options": {"temperature": 0.3, "num_predict": 150},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        reply = json.load(resp)
    return reply["response"].strip(), time.time() - started


def save_analysis(db, stats, advice, secs):
    db.executescript(SCHEMA)
    now = time.time()
    db.execute(
        "INSERT INTO analyses (ts, time, status, stats, advice, ai_secs) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (int(now), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
         stats["status"], json.dumps(stats), advice,
         None if secs is None else round(secs, 1)))
    db.commit()


def show_latest(db):
    db.executescript(SCHEMA)
    row = db.execute("SELECT time, status, stats, advice, ai_secs "
                     "FROM analyses ORDER BY ts DESC LIMIT 1").fetchone()
    if not row:
        print("No analyses saved yet.")
        return
    t, status, stats, advice, secs = row
    print(f"Analysis from {t}   status: {status}")
    print(json.dumps(json.loads(stats), indent=2))
    print(f"\nGemma ({secs} s):\n{advice}" if advice else "\n(no AI advice)")


def main():
    parser = argparse.ArgumentParser(description="AI soil analysis")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip Gemma, rules and stats only")
    parser.add_argument("--show", action="store_true",
                        help="print the latest saved analysis and exit")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH)
    if args.show:
        show_latest(db)
        return

    stats = compute_stats(db)
    print(f"Status (decided by code): {stats['status']}")
    print(json.dumps(stats, indent=2), flush=True)

    advice = secs = None
    if stats["status"] != "NO_DATA" and not args.no_ai:
        prompt = build_prompt(stats)
        print("\nPrompt sent to Gemma:\n" + prompt, flush=True)
        print(f"\nAsking {MODEL} (takes ~30-90 s on a Pi 4)...", flush=True)
        try:
            advice, secs = ask_gemma(prompt)
            print(f"\nGemma's advice ({secs:.0f} s):\n{advice}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"\nGemma unavailable ({e}); saving stats only.")

    save_analysis(db, stats, advice, secs)
    db.close()


if __name__ == "__main__":
    main()
