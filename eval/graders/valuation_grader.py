#!/usr/bin/env python3
"""Valuation grader: audit the internal consistency of the valuation engine output.

The analog of an Excel model audit — it does not re-run the DCF, it checks that
the numbers the engine wrote are mutually consistent and obey the guardrails:

  HARD FAIL (math is wrong):
    - terminal growth >= WACC (Gordon model invalid)
    - sensitivity-grid center cell != base-case intrinsic value
    - margin_of_safety != (intrinsic_base - price) / price
    - intrinsic range not ordered bear <= base <= bull
    - comps blended implied value != mean of its components
  WARN (model fragile, not a failure):
    - terminal value outside 50–70% of enterprise value
    - confidence == "low"

A not-applicable valuation (ETF/fund) passes with a note — there is nothing to audit.
"""

import csv
import json
import sys
from pathlib import Path

# tolerances: engine rounds prices/values to 2 dp and ratios to 4 dp
_ABS_TOL = 0.05      # per-share value comparisons
_MOS_TOL = 0.01      # margin-of-safety (1 percentage point)
_BLEND_TOL = 0.02


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _center_cell(csv_path: Path):
    """Return the middle data cell of the sensitivity grid, or None."""
    try:
        with open(csv_path, newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    data = rows[1:]  # drop header row
    if not data:
        return None
    mid_r = len(data) // 2
    row = data[mid_r][1:]  # drop the wacc label column
    if not row:
        return None
    mid_c = len(row) // 2
    try:
        return float(row[mid_c])
    except (ValueError, IndexError):
        return None


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    summary_path = ws / "valuation" / date / "valuation_summary.json"
    sens_path = ws / "valuation" / date / "dcf_sensitivity.csv"

    result = {
        "grader": "valuation",
        "pass": False,
        "applicable": None,
        "confidence": None,
        "checks": [],       # list of {name, ok, detail}
        "warnings": [],
        "details": [],
        "errors": [],
    }

    summary = load_json(summary_path)
    if summary is None:
        result["errors"].append(f"Valuation summary not found or invalid: {summary_path}")
        return result

    result["applicable"] = summary.get("applicable")
    result["confidence"] = summary.get("confidence")

    # Not applicable (ETF/fund) — nothing to audit.
    if not summary.get("applicable", False):
        result["pass"] = True
        result["details"].append(
            f"Valuation not applicable ({summary.get('reason', 'n/a')}); nothing to audit.")
        return result

    def check(name, ok, detail):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            result["details"].append(f"FAIL [{name}]: {detail}")
        return ok

    hard_ok = True
    dcf = summary.get("dcf") or {}

    # 1. WACC > terminal growth
    if dcf:
        w, g = dcf.get("wacc"), dcf.get("terminal_growth")
        if w is not None and g is not None:
            hard_ok &= check("wacc_gt_terminal_growth", w > g,
                             f"wacc={w}, terminal_growth={g}")

    # 2. intrinsic range ordering bear <= base <= bull
    rng = summary.get("intrinsic_range") or {}
    bear, base, bull = rng.get("bear"), rng.get("base"), rng.get("bull")
    if None not in (bear, base, bull):
        hard_ok &= check("intrinsic_range_ordered", bear <= base <= bull,
                         f"bear={bear}, base={base}, bull={bull}")

    # 3. sensitivity center cell == base intrinsic value
    base_iv = summary.get("intrinsic_value_base")
    if base_iv is not None and sens_path.exists():
        center = _center_cell(sens_path)
        if center is not None:
            hard_ok &= check("sensitivity_center_eq_base",
                             abs(center - base_iv) <= _ABS_TOL,
                             f"center={center}, base={base_iv}")

    # 4. margin of safety == (intrinsic_base - price) / price
    mos = summary.get("margin_of_safety")
    price = summary.get("current_price")
    if mos is not None and base_iv is not None and price:
        recomputed = (base_iv - price) / price
        hard_ok &= check("margin_of_safety_recompute",
                         abs(recomputed - mos) <= _MOS_TOL,
                         f"stored={mos}, recomputed={recomputed:.4f}")

    # 5. comps blended implied value == mean of its components
    imp = summary.get("comps_implied_value") or {}
    comps = [imp.get("by_pe"), imp.get("by_ev_ebitda")]
    present = [v for v in comps if isinstance(v, (int, float)) and v > 0]
    if imp.get("blended") is not None and present:
        mean = sum(present) / len(present)
        hard_ok &= check("comps_blended_is_mean",
                         abs(mean - imp["blended"]) <= max(_BLEND_TOL, 0.005 * mean),
                         f"blended={imp['blended']}, mean={mean:.4f}")

    # ── warnings (do not fail) ────────────────────────────────────────────────
    if dcf and dcf.get("tv_fraction_in_band") is False:
        base_tv = (dcf.get("scenarios", {}).get("base", {}) or {}).get("tv_fraction")
        result["warnings"].append(
            f"terminal value fraction {base_tv} outside 50–70% band (model leans on terminal value)")
    if summary.get("confidence") == "low":
        result["warnings"].append(
            f"low confidence; inputs_missing={summary.get('inputs_missing')}")

    result["pass"] = hard_ok
    if hard_ok:
        result["details"].append(
            f"All {len(result['checks'])} consistency checks passed"
            + (f"; {len(result['warnings'])} warning(s)" if result["warnings"] else ""))
    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace, date = sys.argv[1], sys.argv[2]
    result = grade(workspace, date)

    out_dir = Path(workspace) / "eval" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "valuation_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
