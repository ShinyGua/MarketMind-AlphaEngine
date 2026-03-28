#!/usr/bin/env python3
"""Factuality grader: verify quant numbers in the final report match quant_summary.json."""

import sys
import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import detect_final_report, grader_result_path


def load_json(path: Path) -> dict | list | None:
    """Load JSON file, return None if missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return None


def extract_quant_values(quant: dict) -> list[dict]:
    """Flatten quant_summary.json into a list of {field, value, category} dicts."""
    entries = []

    if "latest_close" in quant:
        entries.append({"field": "latest_close", "value": quant["latest_close"], "category": "price"})

    for period, val in (quant.get("returns") or {}).items():
        entries.append({"field": f"return_{period}", "value": val, "category": "percentage"})

    tech = quant.get("technical") or {}
    indicator_fields = [
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "sma_20", "sma_50", "ema_12", "ema_26", "atr_14",
    ]
    for field in indicator_fields:
        if field in tech:
            cat = "price" if field.startswith(("sma_", "ema_", "atr_")) else "indicator"
            entries.append({"field": field, "value": tech[field], "category": cat})

    return entries


def search_price_in_report(text: str, value: float, tolerance: float = 0.5) -> float | None:
    """Search for $XXX.XX patterns and return the closest match within tolerance."""
    matches = re.findall(r"\$\s*([\d,]+\.?\d*)", text)
    best = None
    best_diff = tolerance + 1
    for m in matches:
        try:
            num = float(m.replace(",", ""))
            diff = abs(num - value)
            if diff <= tolerance and diff < best_diff:
                best = num
                best_diff = diff
        except ValueError:
            continue
    return best


def search_percentage_in_report(text: str, value: float, tolerance: float = 0.1) -> float | None:
    """Search for [+-]X.XX% patterns and return the closest match within tolerance."""
    matches = re.findall(r"([+-]?\d+\.?\d*)%", text)
    best = None
    best_diff = tolerance + 1
    for m in matches:
        try:
            num = float(m)
            diff = abs(num - value)
            if diff <= tolerance and diff < best_diff:
                best = num
                best_diff = diff
        except ValueError:
            continue
    return best


def search_indicator_in_report(text: str, field: str, value: float, tolerance: float = 0.5) -> float | None:
    """Search for a numeric value near the indicator name in the report."""
    # Build name variants for searching context
    name_map = {
        "rsi_14": ["RSI", "RSI(14)", "RSI 14"],
        "macd": ["MACD"],
        "macd_signal": ["MACD Signal", "signal line", "MACD signal"],
        "macd_hist": ["MACD Histogram", "histogram", "MACD hist"],
        "sma_20": ["SMA(20)", "SMA 20", "SMA20"],
        "sma_50": ["SMA(50)", "SMA 50", "SMA50"],
        "ema_12": ["EMA(12)", "EMA 12", "EMA12"],
        "ema_26": ["EMA(26)", "EMA 26", "EMA26"],
        "atr_14": ["ATR(14)", "ATR 14", "ATR14"],
    }
    names = name_map.get(field, [field])

    for name in names:
        # Find all occurrences of the indicator name, then search nearby for the value
        for match in re.finditer(re.escape(name), text, re.IGNORECASE):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            snippet = text[start:end]

            # Extract all numbers from the snippet
            nums = re.findall(r"[+-]?\d+\.?\d*", snippet)
            for n in nums:
                try:
                    num = float(n)
                    if abs(num - value) <= tolerance:
                        return num
                except ValueError:
                    continue

    # Fallback: search entire text for the exact value (rounded to 2 decimal places)
    rounded = round(value, 2)
    pattern = re.escape(f"{rounded}")
    if re.search(pattern, text):
        return rounded

    return None


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    report_path = detect_final_report(ws, date)
    quant_path = ws / "quant" / date / "quant_summary.json"

    result = {
        "grader": "factuality",
        "pass": False,
        "checks": [],
        "total_checks": 0,
        "passed_checks": 0,
        "errors": [],
    }

    report_text = None
    if report_path is None:
        result["errors"].append(f"Report not found under: {ws / 'final' / date}")
    else:
        try:
            report_text = report_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            result["errors"].append(f"Report not found: {report_path}")

    quant = load_json(quant_path)
    if quant is None:
        result["errors"].append(f"Quant summary not found or invalid: {quant_path}")

    if report_text is None or quant is None:
        return result

    entries = extract_quant_values(quant)
    result["total_checks"] = len(entries)

    for entry in entries:
        field = entry["field"]
        source_value = entry["value"]
        category = entry["category"]
        report_value = None

        if category == "price":
            # Try dollar pattern first, then indicator search
            report_value = search_price_in_report(report_text, source_value, tolerance=0.5)
            if report_value is None:
                report_value = search_indicator_in_report(report_text, field, source_value, tolerance=0.5)
        elif category == "percentage":
            report_value = search_percentage_in_report(report_text, source_value, tolerance=0.1)
        elif category == "indicator":
            report_value = search_indicator_in_report(report_text, field, source_value, tolerance=0.5)

        matched = report_value is not None
        if matched:
            result["passed_checks"] += 1

        check = {
            "field": field,
            "source_value": source_value,
            "report_value": report_value,
            "match": matched,
        }
        result["checks"].append(check)

    if result["total_checks"] > 0:
        ratio = result["passed_checks"] / result["total_checks"]
        result["pass"] = ratio >= 0.8
    else:
        result["pass"] = True

    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    date = sys.argv[2]

    result = grade(workspace, date)

    # Write output
    ws = Path(workspace)
    out_path = grader_result_path(ws, date, "factuality")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
