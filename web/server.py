#!/usr/bin/env python3
"""
MarketMind-AlphaEngine Web Dashboard
Single-process Flask server — serves the full UI and API from one command.
Usage: .venv/bin/python3 web/server.py [--port 7860] [--no-browser]
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.parent   # project root (no hardcoded path)
sys.path.insert(0, str(ROOT / "mcp"))
from shared.contracts import detect_final_report  # noqa: E402

from flask import Flask, abort, jsonify, request, send_file
WORKSPACES = ROOT / "workspaces"

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _safe_path(base: Path, *parts: str) -> Path:
    """Resolve a path and abort with 404 if it escapes the base directory."""
    p = (base / Path(*parts)).resolve()
    if not p.is_relative_to(base.resolve()):
        abort(404)
    return p


def _read_json(path: Path) -> dict | list | None:
    if path.exists():
        return json.loads(path.read_text())
    return None


def _latest_date(ticker_dir: Path) -> str | None:
    """Return the most recent YYYY-MM-DD that has a final report."""
    final_dir = ticker_dir / "final"
    if not final_dir.exists():
        return None
    dates = sorted(
        d.name for d in final_dir.iterdir()
        if d.is_dir() and detect_final_report(ticker_dir, d.name) is not None
    )
    return dates[-1] if dates else None


def _available_dates(ticker_dir: Path) -> list[str]:
    """All YYYY-MM-DD dates that have a final report, newest first."""
    final_dir = ticker_dir / "final"
    if not final_dir.exists():
        return []
    return sorted(
        (d.name for d in final_dir.iterdir()
         if d.is_dir() and detect_final_report(ticker_dir, d.name) is not None),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# SPA entry point
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/tickers")
def list_tickers():
    """List all tickers with their latest report summary."""
    result = []
    if not WORKSPACES.exists():
        return jsonify(result)

    for ticker_dir in sorted(WORKSPACES.iterdir()):
        if not ticker_dir.is_dir() or ticker_dir.name == "shared":
            continue

        ticker = ticker_dir.name
        latest = _latest_date(ticker_dir)
        if not latest:
            continue

        profile = _read_json(ticker_dir / "profile" / "company_profile.json") or {}
        decision = _read_json(ticker_dir / "decision" / latest / "final_decision.json") or {}
        status = _read_json(ticker_dir / "status.json") or {}

        result.append({
            "ticker": ticker,
            "name": profile.get("name", ticker),
            "exchange": profile.get("exchange", ""),
            "sector": profile.get("sector", ""),
            "market_profile": profile.get("market_profile", ""),
            "latest_date": latest,
            "decision": decision.get("decision", ""),
            "confidence": decision.get("confidence", None),
            "stage": status.get("stage", ""),
        })

    return jsonify(result)


@app.route("/api/tickers/<ticker>")
def ticker_dates(ticker: str):
    """List all available report dates for a ticker, newest first."""
    ticker_dir = _safe_path(WORKSPACES, ticker)
    if not ticker_dir.is_dir():
        abort(404)

    dates = _available_dates(ticker_dir)
    profile = _read_json(ticker_dir / "profile" / "company_profile.json") or {}

    rows = []
    for date in dates:
        decision = _read_json(ticker_dir / "decision" / date / "final_decision.json") or {}
        rows.append({
            "date": date,
            "decision": decision.get("decision", ""),
            "confidence": decision.get("confidence", None),
            "has_pdf": (ticker_dir / "exports" / date / "pdf" / "report.pdf").exists(),
        })

    return jsonify({
        "ticker": ticker,
        "name": profile.get("name", ticker),
        "exchange": profile.get("exchange", ""),
        "sector": profile.get("sector", ""),
        "dates": rows,
    })


@app.route("/api/tickers/<ticker>/<date>/report")
def ticker_report(ticker: str, date: str):
    """Return the markdown report as plain text."""
    ticker_dir = _safe_path(WORKSPACES, ticker)
    if not ticker_dir.is_dir():
        abort(404)
    path = detect_final_report(ticker_dir, date)
    if path is None:
        abort(404)
    return path.read_text(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/tickers/<ticker>/<date>/decision")
def ticker_decision(ticker: str, date: str):
    path = _safe_path(WORKSPACES, ticker, "decision", date, "final_decision.json")
    if not path.exists():
        abort(404)
    return jsonify(_read_json(path))


@app.route("/api/tickers/<ticker>/<date>/quant")
def ticker_quant(ticker: str, date: str):
    path = _safe_path(WORKSPACES, ticker, "quant", date, "quant_summary.json")
    if not path.exists():
        abort(404)
    return jsonify(_read_json(path))


@app.route("/api/tickers/<ticker>/<date>/chips")
def ticker_chips(ticker: str, date: str):
    path = _safe_path(WORKSPACES, ticker, "quant", date, "chip_structure.json")
    if not path.exists():
        abort(404)
    return jsonify(_read_json(path))


@app.route("/api/tickers/<ticker>/<date>/pdf")
def ticker_pdf(ticker: str, date: str):
    path = _safe_path(WORKSPACES, ticker, "exports", date, "pdf", "report.pdf")
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="application/pdf")


# ── Eval API ──────────────────────────────────────────────────────────

@app.route("/api/eval/runs")
def eval_runs():
    """Return recent pipeline run log entries."""
    run_log = ROOT / "logs" / "run_log.jsonl"
    if not run_log.exists():
        return jsonify([])
    entries = []
    limit = request.args.get("limit", 50, type=int)
    ticker = request.args.get("ticker", "").upper()
    for line in run_log.read_text().strip().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if ticker and entry.get("ticker", "").upper() != ticker:
                continue
            entries.append(entry)
        except json.JSONDecodeError:
            continue
    return jsonify(entries[-limit:])


@app.route("/api/eval/metrics")
def eval_metrics():
    """Return aggregate evaluation metrics."""
    import subprocess
    ticker_arg = request.args.get("ticker", "")
    cmd = [str(ROOT / ".venv" / "bin" / "python3"), str(ROOT / "eval" / "metrics.py"), "--format", "json"]
    if ticker_arg:
        cmd.extend(["--ticker", ticker_arg])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=str(ROOT))
        if result.returncode == 0 and result.stdout.strip():
            return app.response_class(result.stdout, mimetype="application/json")
        return jsonify({"error": "No metrics available"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/eval/ticker/<ticker>")
def eval_ticker(ticker):
    """Return evaluation history for a specific ticker."""
    ticker = ticker.upper()
    run_log = ROOT / "logs" / "run_log.jsonl"
    if not run_log.exists():
        return jsonify({"ticker": ticker, "runs": []})
    runs = []
    for line in run_log.read_text().strip().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("ticker", "").upper() == ticker:
                runs.append({
                    "run_date": entry.get("run_date"),
                    "decision": entry.get("decision") or entry.get("final_decision"),
                    "confidence": entry.get("confidence") or entry.get("final_confidence"),
                    "overall_score": (entry.get("final_review_scores") or entry.get("final_scores", {})).get("overall"),
                    "release_status": entry.get("release_status"),
                    "success": entry.get("release_status", "passed") != "failed",
                    "user_review": entry.get("user_review"),
                    "duration_s": entry.get("total_duration_s"),
                    "review_iterations": entry.get("review_iterations"),
                })
        except json.JSONDecodeError:
            continue
    return jsonify({"ticker": ticker, "runs": runs})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MarketMind Web Dashboard")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"MarketMind Dashboard → {url}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        import threading
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=args.port, debug=False)
