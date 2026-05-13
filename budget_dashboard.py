"""
budget_dashboard.py - Budget runway HTTP server (stdlib only, localhost-bound).

Reads ~/.claude/usage.db (populated by scanner.py / cli.py scan), aggregates
month-to-date spend against a configured monthly budget, and serves a single
HTML page + three JSON endpoints. Re-scans on every API request so new
Claude Code sessions appear within seconds.
"""

import calendar
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Pricing fallback for rows where costUSD wasn't recorded (pre-v2.1.97).
# Kept in sync with cli.py's PRICING table.
PRICING = {
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
}

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "budget_config.json"
CONFIG_EXAMPLE_PATH = ROOT / "budget_config.example.json"
CALIBRATION_PATH = ROOT / "calibration.json"
PUBLIC_DIR = ROOT / "public"
VENDOR_DIR = PUBLIC_DIR / "vendor"


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    """Load budget_config.json, copying from the example on first run."""
    if not CONFIG_PATH.exists():
        if not CONFIG_EXAMPLE_PATH.exists():
            sys.exit("Missing budget_config.example.json — re-clone the repo.")
        shutil.copy(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        print(f"Created {CONFIG_PATH.name} from example.")
        print(f"  Edit it to set your monthly budget before reading the dashboard.")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    cfg["db_path"] = Path(os.path.expanduser(cfg["db_path"]))
    return cfg


def load_calibration():
    if not CALIBRATION_PATH.exists():
        return None
    try:
        with open(CALIBRATION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ── Pricing fallback (for rows where cost_usd is NULL) ───────────────────────

def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus" in m:   return PRICING["claude-opus-4-7"]
    if "sonnet" in m: return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:  return PRICING["claude-haiku-4-5"]
    return None


def calc_cost_from_tokens(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        (inp or 0)            * p["input"]       / 1_000_000 +
        (out or 0)            * p["output"]      / 1_000_000 +
        (cache_read or 0)     * p["cache_read"]  / 1_000_000 +
        (cache_creation or 0) * p["cache_write"] / 1_000_000
    )


# ── Workday math ─────────────────────────────────────────────────────────────

def count_workdays(start_d, end_d):
    """Count Mon–Fri inclusive between two dates."""
    if end_d < start_d:
        return 0
    n = 0
    d = start_d
    while d <= end_d:
        if d.weekday() < 5:  # Mon=0..Fri=4
            n += 1
        d += timedelta(days=1)
    return n


def month_bounds(today):
    """First and last day of today's month."""
    first = today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    last = today.replace(day=last_day)
    return first, last


# ── DB queries ───────────────────────────────────────────────────────────────

def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def spend_for_period(conn, start_iso, end_iso):
    """Total spend between two YYYY-MM-DD dates (inclusive). Uses cost_usd
    when present, falls back to per-row token-pricing otherwise."""
    rows = conn.execute("""
        SELECT model, cost_usd,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start_iso, end_iso)).fetchall()

    total = 0.0
    for r in rows:
        if r["cost_usd"] is not None:
            total += r["cost_usd"]
        else:
            total += calc_cost_from_tokens(
                r["model"], r["input_tokens"], r["output_tokens"],
                r["cache_read_tokens"], r["cache_creation_tokens"],
            )
    return total


def daily_breakdown(conn, start_iso, end_iso):
    """Per-day spend and session count between two dates (inclusive)."""
    rows = conn.execute("""
        SELECT substr(timestamp, 1, 10) AS day,
               model, cost_usd,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
               session_id
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start_iso, end_iso)).fetchall()

    per_day = {}
    for r in rows:
        d = r["day"]
        bucket = per_day.setdefault(d, {"spend": 0.0, "sessions": set()})
        bucket["sessions"].add(r["session_id"])
        if r["cost_usd"] is not None:
            bucket["spend"] += r["cost_usd"]
        else:
            bucket["spend"] += calc_cost_from_tokens(
                r["model"], r["input_tokens"], r["output_tokens"],
                r["cache_read_tokens"], r["cache_creation_tokens"],
            )
    return per_day


def model_breakdown(conn, start_iso, end_iso):
    """Spend, tokens, and percentage by model family (opus/sonnet/haiku)."""
    rows = conn.execute("""
        SELECT model, cost_usd,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start_iso, end_iso)).fetchall()

    families = {"opus": {"cost": 0.0, "tokens": 0},
                "sonnet": {"cost": 0.0, "tokens": 0},
                "haiku": {"cost": 0.0, "tokens": 0},
                "other": {"cost": 0.0, "tokens": 0}}

    for r in rows:
        family = "other"
        m = (r["model"] or "").lower()
        if "opus" in m:   family = "opus"
        elif "sonnet" in m: family = "sonnet"
        elif "haiku" in m: family = "haiku"

        if r["cost_usd"] is not None:
            cost = r["cost_usd"]
        else:
            cost = calc_cost_from_tokens(
                r["model"], r["input_tokens"], r["output_tokens"],
                r["cache_read_tokens"], r["cache_creation_tokens"],
            )
        families[family]["cost"] += cost
        families[family]["tokens"] += (
            (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
            + (r["cache_read_tokens"] or 0) + (r["cache_creation_tokens"] or 0)
        )

    total = sum(f["cost"] for f in families.values())
    result = []
    for name in ("opus", "sonnet", "haiku", "other"):
        f = families[name]
        if f["cost"] == 0 and f["tokens"] == 0 and name == "other":
            continue
        result.append({
            "model": name,
            "total_cost": round(f["cost"], 2),
            "total_tokens": f["tokens"],
            "percentage": round((f["cost"] / total) * 100, 1) if total > 0 else 0.0,
        })
    return result


def sessions_in_month(conn, start_iso, end_iso):
    row = conn.execute("""
        SELECT COUNT(DISTINCT session_id) AS n
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start_iso, end_iso)).fetchone()
    return row["n"] or 0


# ── Calibration drift ────────────────────────────────────────────────────────

def calibration_drift_pct(cal):
    """Average drift % across calibration entries.
    Positive % => local estimate is below invoice (under-estimating)."""
    if not cal:
        return None
    entries = cal.get("calibrations") or []
    drifts = []
    for e in entries:
        actual = e.get("actual_spend")
        local = e.get("local_estimate")
        if actual and local:
            drifts.append((actual - local) / actual * 100)
    if not drifts:
        return None
    return round(sum(drifts) / len(drifts), 2)


# ── Summary builder ──────────────────────────────────────────────────────────

def build_summary(cfg):
    today = date.today()
    first, last = month_bounds(today)
    start_iso, end_iso, today_iso = first.isoformat(), last.isoformat(), today.isoformat()

    with _connect(cfg["db_path"]) as conn:
        mtd_spend = spend_for_period(conn, start_iso, today_iso)
        today_spend = spend_for_period(conn, today_iso, today_iso)
        sessions = sessions_in_month(conn, start_iso, today_iso)

    total_workdays = count_workdays(first, last)
    workdays_elapsed = count_workdays(first, today)
    workdays_remaining = max(1, total_workdays - workdays_elapsed)

    monthly_budget = cfg["monthly_budget"]
    daily_budget = (monthly_budget - mtd_spend) / workdays_remaining
    todays_runway = daily_budget - today_spend

    avg_workday_spend = mtd_spend / max(1, workdays_elapsed)
    avg_daily_spend = mtd_spend / max(1, (today - first).days + 1)
    projected_total = mtd_spend + (avg_workday_spend * workdays_remaining)
    surplus = monthly_budget - projected_total

    if surplus > 50:
        status = "green"
    elif surplus >= 0:
        status = "yellow"
    else:
        status = "red"

    projected_dry_date = None
    if status == "red" and avg_workday_spend > 0:
        # Walk forward day by day from today; on each remaining workday add
        # avg_workday_spend. The date where cumulative MTD spend would exceed
        # the monthly budget is the dry date.
        running = mtd_spend
        d = today
        for _ in range(total_workdays + 5):
            d += timedelta(days=1)
            if d.weekday() < 5:
                running += avg_workday_spend
            if running >= monthly_budget:
                projected_dry_date = d.isoformat()
                break

    drift = calibration_drift_pct(load_calibration())

    return {
        "monthly_budget": monthly_budget,
        "month_to_date_spend": round(mtd_spend, 2),
        "today_spend": round(today_spend, 2),
        "daily_budget": round(daily_budget, 2),
        "todays_runway": round(todays_runway, 2),
        "workdays_remaining": workdays_remaining,
        "workdays_elapsed": workdays_elapsed,
        "total_workdays": total_workdays,
        "sessions_this_month": sessions,
        "avg_daily_spend": round(avg_daily_spend, 2),
        "projected_month_end": round(projected_total, 2),
        "projected_surplus": round(surplus, 2),
        "surplus_status": status,
        "projected_dry_date": projected_dry_date,
        "calibration_drift_pct": drift,
        "currency_symbol": cfg.get("currency_symbol", "$"),
        "refresh_interval_seconds": cfg.get("refresh_interval_seconds", 30),
        "today": today_iso,
    }


def build_daily(cfg):
    today = date.today()
    first, last = month_bounds(today)
    with _connect(cfg["db_path"]) as conn:
        per_day = daily_breakdown(conn, first.isoformat(), last.isoformat())

    out = []
    d = first
    while d <= last:
        iso = d.isoformat()
        bucket = per_day.get(iso, {"spend": 0.0, "sessions": set()})
        out.append({
            "date": iso,
            "day_of_week": d.strftime("%a"),
            "is_weekend": d.weekday() >= 5,
            "spend": round(bucket["spend"], 2),
            "sessions": len(bucket["sessions"]) if isinstance(bucket["sessions"], set) else bucket["sessions"],
            "is_today": d == today,
            "is_future": d > today,
        })
        d += timedelta(days=1)
    return out


def build_models(cfg):
    today = date.today()
    first, _ = month_bounds(today)
    with _connect(cfg["db_path"]) as conn:
        return model_breakdown(conn, first.isoformat(), today.isoformat())


# ── HTTP handler ─────────────────────────────────────────────────────────────

def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, content_type):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _rescan(self):
            """Run cli.py scan in a subprocess so we pick up new sessions."""
            try:
                subprocess.run(
                    [sys.executable, str(ROOT / "cli.py"), "scan"],
                    capture_output=True, timeout=30, check=False,
                )
            except (subprocess.SubprocessError, OSError):
                pass  # Scan failure shouldn't block the dashboard.

        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    self._send_file(PUBLIC_DIR / "index.html", "text/html; charset=utf-8")
                    return

                if self.path.startswith("/api/vendor/"):
                    name = self.path[len("/api/vendor/"):]
                    # Block path traversal.
                    if "/" in name or "\\" in name or name.startswith("."):
                        self.send_response(404); self.end_headers(); return
                    target = VENDOR_DIR / name
                    if not target.is_file():
                        self.send_response(404); self.end_headers(); return
                    ctype = "application/javascript" if name.endswith(".js") else "application/octet-stream"
                    self._send_file(target, ctype)
                    return

                if self.path == "/api/summary":
                    self._rescan()
                    self._send_json(build_summary(cfg))
                    return

                if self.path == "/api/daily":
                    self._rescan()
                    self._send_json(build_daily(cfg))
                    return

                if self.path == "/api/models":
                    self._rescan()
                    self._send_json(build_models(cfg))
                    return

                self.send_response(404); self.end_headers()
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

    return Handler


def serve():
    cfg = load_config()
    if not cfg["db_path"].exists():
        print(f"Note: {cfg['db_path']} not found yet — will be created on first scan.")
        print(f"      Run: python cli.py scan")

    port = int(cfg.get("port", 8099))
    server = HTTPServer(("127.0.0.1", port), make_handler(cfg))
    print(f"CC Budget Dashboard running at http://127.0.0.1:{port}")
    print(f"Monthly budget: {cfg.get('currency_symbol', '$')}{cfg['monthly_budget']}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
