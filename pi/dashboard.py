#!/usr/bin/env python3
"""Tiny offline web dashboard for the soil monitor.

Uses only the Python standard library: no Flask, no internet, no CDN.
    python3 dashboard.py            # http://<pi-ip>:8080
    python3 dashboard.py --port 9000

Endpoints
    GET  /                       the dashboard page (web/index.html)
    GET  /api/readings?hours=24  readings for the chart (+ raw ADC value)
    GET  /api/readings.csv?hours=24   same, as a CSV download
    GET  /api/latest             latest reading, status, latest AI analysis
    POST /api/analyze            run analyzer.py now (in the background)
    POST /api/chat               ask Gemma a question; answer streams back
"""

import argparse
import json
import csv
import io
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from analyzer import (DRY_BELOW, MODEL, SCHEMA as ANALYSES_SCHEMA, WET_ABOVE,
                      compute_stats, moisture_status)
from sensor import FULL_SCALE_VOLTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "moisture.db")
INDEX_HTML = os.path.join(BASE_DIR, "web", "index.html")
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

_analysis_proc = None            # the running analyzer.py, if any
_chat_lock = threading.Lock()    # one chat answer at a time (Pi has ~1 GB free)


def db_connect():
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    return db


def adc_raw(volts):
    """Back from volts to the ADC's own number (16-bit scale, ±4.096 V)."""
    return round(volts * 32768 / FULL_SCALE_VOLTS)


def get_readings(hours):
    since = int(time.time()) - int(hours * 3600)
    with closing(db_connect()) as db:
        rows = db.execute(
            "SELECT ts, time, percent, volts FROM readings "
            "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [{"ts": ts, "time": t, "percent": p, "volts": v,
             "adc_raw": adc_raw(v)} for ts, t, p, v in rows]


def readings_csv(hours):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time", "unix_ts", "adc_raw", "volts", "moisture_percent"])
    for r in get_readings(hours):
        w.writerow([r["time"], r["ts"], r["adc_raw"], r["volts"],
                    "" if r["percent"] is None else r["percent"]])
    return out.getvalue().encode()


def get_latest():
    with closing(db_connect()) as db:
        r = db.execute("SELECT ts, time, percent, volts FROM readings "
                       "ORDER BY ts DESC LIMIT 1").fetchone()
        has_analyses = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'analyses'").fetchone()
        a = None
        if has_analyses:
            a = db.execute("SELECT time, status, stats, advice, ai_secs "
                           "FROM analyses ORDER BY ts DESC LIMIT 1").fetchone()
    latest = {
        "thresholds": {"dry_below": DRY_BELOW, "wet_above": WET_ABOVE},
        "analysis_running": _analysis_proc is not None
        and _analysis_proc.poll() is None,
        "reading": None,
        "analysis": None,
    }
    if r:
        latest["reading"] = {"ts": r[0], "time": r[1], "percent": r[2],
                             "volts": r[3], "status": moisture_status(r[2])}
    if a:
        latest["analysis"] = {"time": a[0], "status": a[1],
                              "stats": json.loads(a[2]), "advice": a[3],
                              "ai_secs": a[4]}
    return latest


def start_analysis():
    global _analysis_proc
    if _analysis_proc is not None and _analysis_proc.poll() is None:
        return False   # already running: don't load Gemma twice
    _analysis_proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "analyzer.py")],
        cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def current_stats():
    """Fresh stats from the database, rebuilt for every question."""
    with closing(db_connect()) as db:
        return compute_stats(db)


WATER_NOW = {"DRY": "YES", "OK": "NO", "WET": "NO (too wet, let it dry)"}
# Plain words only: listing "DRY<30%" next to an OK status made Gemma say "DRY".
STATUS_WORDS = {"DRY": "DRY (too dry)", "OK": "OK (healthy)",
                "WET": "WET (too wet)"}


def chat_system_prompt(s):
    """Short on purpose: the Pi reads the prompt at only ~12 tokens/s, so
    every word here costs time before Gemma can start answering.
    (The first version was 268 tokens = 22 s; this one is ~90.)"""
    if s["status"] == "NO_DATA":
        return ("Soil moisture monitor. No readings in the last 24 h; the "
                "sensor may be offline. Say so briefly.")
    trend = s.get("trend_percent_per_hour")
    data = [
        f"moisture {s['current_percent']}% ({s['last_reading_minutes_ago']} min ago)",
        f"status {STATUS_WORDS[s['status']]}",
        f"water now: {WATER_NOW[s['status']]}",
        f"24h min {s['min_24h']}% max {s['max_24h']}% avg {s['avg_24h']}%",
        "trend " + ("unknown" if trend is None else f"{trend:+.1f}%/h"),
    ]
    if "hours_until_dry" in s:
        data.append(f"dry in ~{s['hours_until_dry']}h")
    if "last_watered_hours_ago" in s:
        data.append(f"watered {s['last_watered_hours_ago']}h ago")
    return ("Potted plant soil monitor. Data: " + "; ".join(data) + ".\n"
            "Answer in 1-3 short sentences using only this data. "
            "Never change the status or the water decision.")


# Simple questions with an exact answer skip the AI entirely (< 0.1 s).
_FOLLOW_UP_WORDS = re.compile(
    r"\b(why|explain|mean|how come|when|will|tomorrow|today|yesterday|trend|"
    r"change|changed|history|how much|how often|predict|week)\b")
_STATUS_Q = re.compile(r"\b(status|right now|currently|current|moisture now|"
                       r"how (wet|dry|moist))\b")
_WATER_Q = re.compile(r"\b((should|do|must|can) i water|need(s)? (water|watering)|"
                      r"water (it|the plant)|time to water)\b")


def instant_answer(question, s):
    q = question.lower()
    if _FOLLOW_UP_WORDS.search(q):
        return None   # needs reasoning or history: let Gemma answer
    is_water, is_status = bool(_WATER_Q.search(q)), bool(_STATUS_Q.search(q))
    if not (is_water or is_status):
        return None
    if s["status"] == "NO_DATA":
        return ("I have no readings from the last 24 hours, so I can't tell. "
                "Check that the sensor and the collector are running.")
    pct, ago = s["current_percent"], s["last_reading_minutes_ago"]
    if is_water:
        return {
            "DRY": f"Yes, water the plant now: moisture is {pct}%, below the "
                   f"{DRY_BELOW:.0f}% dry limit.",
            "OK": f"No, not right now: moisture is {pct}%, inside the healthy "
                  f"{DRY_BELOW:.0f}-{WET_ABOVE:.0f}% range.",
            "WET": f"No, don't water: moisture is {pct}%, above {WET_ABOVE:.0f}%. "
                   "Let the soil drain and dry out first.",
        }[s["status"]]
    meaning = {"DRY": f"below {DRY_BELOW:.0f}%, the plant needs water",
               "OK": f"between {DRY_BELOW:.0f}% and {WET_ABOVE:.0f}%, healthy",
               "WET": f"above {WET_ABOVE:.0f}%, too wet"}[s["status"]]
    return (f"Moisture is {pct}% (reading from {ago} min ago), so the status "
            f"is {s['status']}: {meaning}.")


def clean_history(history):
    """Keep the last 3 exchanges, only valid roles, trimmed text."""
    out = []
    for m in history[-6:] if isinstance(history, list) else []:
        if (isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)):
            out.append({"role": m["role"], "content": m["content"][:1000]})
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                with open(INDEX_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path in ("/api/readings", "/api/readings.csv"):
                hours = float(parse_qs(url.query).get("hours", ["24"])[0])
                hours = max(0.5, min(hours, 24 * 365))
                if url.path.endswith(".csv"):
                    data = readings_csv(hours)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", "attachment; "
                                     f'filename="soil-readings-{hours:g}h.csv"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send(200, get_readings(hours))
            elif url.path == "/api/latest":
                self._send(200, get_latest())
            else:
                self._send(404, {"error": "not found"})
        except (sqlite3.Error, ValueError) as e:
            self._send(500, {"error": str(e)})

    def _chat(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 20000:
                return self._send(413, {"error": "message too long"})
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad JSON"})
        question = str(body.get("question", "")).strip()[:500]
        if not question:
            return self._send(400, {"error": "empty question"})

        try:
            stats = current_stats()
        except sqlite3.Error as e:
            return self._send(500, {"error": str(e)})

        quick = instant_answer(question, stats)
        if quick:
            data = quick.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Answer-Source", "instant")
            self.end_headers()
            self.wfile.write(data)
            return

        if not _chat_lock.acquire(blocking=False):
            return self._send(429, {"error": "Gemma is busy answering "
                                             "someone else. Try again soon."})
        streaming = False
        try:
            messages = ([{"role": "system", "content": chat_system_prompt(stats)}]
                        + clean_history(body.get("history"))
                        + [{"role": "user", "content": question}])
            req = urllib.request.Request(
                OLLAMA_CHAT_URL,
                data=json.dumps({
                    "model": MODEL, "messages": messages, "stream": True,
                    "keep_alive": "5m",   # stay loaded for follow-up questions
                    "options": {"temperature": 0.3, "num_predict": 200},
                }).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                # Stream each word to the browser as Gemma writes it
                # (~2.6 tokens/s, so waiting for the whole answer feels slow).
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Answer-Source", "gemma")
                self.end_headers()
                streaming = True
                for line in resp:
                    chunk = json.loads(line)
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        self.wfile.write(text.encode())
                        self.wfile.flush()
                    if chunk.get("done"):
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass   # the browser closed the page mid-answer
        except (urllib.error.URLError, OSError, sqlite3.Error,
                json.JSONDecodeError) as e:
            if streaming:
                self.wfile.write(f"\n[Gemma error: {e}]".encode())
            else:
                self._send(503, {"error": f"Gemma unavailable: {e}"})
        finally:
            _chat_lock.release()

    def do_POST(self):
        if urlparse(self.path).path == "/api/chat":
            return self._chat()
        if urlparse(self.path).path == "/api/analyze":
            started = start_analysis()
            self._send(202, {"started": started,
                             "message": "Gemma is thinking (~1 min)"
                             if started else "Already running"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass   # keep the journal quiet; errors still raise


def main():
    parser = argparse.ArgumentParser(description="Soil monitor dashboard")
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 = reachable from your network")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    # Make sure the analyses table exists so the page works on day one.
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript(ANALYSES_SCHEMA)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
