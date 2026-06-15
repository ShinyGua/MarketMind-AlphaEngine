#!/usr/bin/env python3
"""Valuation grader: audit the internal consistency of the valuation engine output.

The analog of an Excel model audit — it does not re-run the DCF, it checks that
the numbers the engine wrote are mutually consistent and obey the guardrails:

  HARD FAIL (math is wrong):
    - terminal growth >= WACC (Gordon model invalid)
    - sensitivity-grid center cell != base-case intrinsic value
    - margin_of_safety != (fair_value - price) / price
    - intrinsic range not ordered bear <= base <= bull
    - fair_value_range not ordered low <= base <= high
    - comps blended implied value != mean of its components
  WARN (model fragile, not a failure):
    - terminal value outside 50–70% of enterprise value
    - confidence == "low"
    - non-US/non-USD name discounted with a US risk-free (currency mismatch)

A not-applicable valuation (ETF/fund) passes with a note — there is nothing to audit.
"""

import csv
import json
import sys
from pathlib import Path

# Reuse the engine's own confidence derivation so this check can never drift from
# what the engine actually does (valuation/ has no heavy deps — safe to import).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "valuation"))
from run_valuation import _component_confidence, _CONF_ORD  # noqa: E402

# tolerances: engine rounds prices/values to 2 dp and ratios to 4 dp
_ABS_TOL = 0.05      # per-share value comparisons
_MOS_TOL = 0.01      # margin-of-safety (1 percentage point)
_BLEND_TOL = 0.02
_REL_TOL = 1e-4      # 0.01% — large-value sensitivity-grid center vs base (non-USD safe)


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


_RF_BAND = (0.001, 0.10)   # same sanity band the engine enforces
_RF_STALE_TOL = 0.0005     # 5bp drift between summary rf and the live regime value


def _check_risk_free(summary: dict, ws: Path, date: str, check, result: dict) -> bool:
    """Hard: rf within the sanity band when the DCF ran. Warning: live 10Y was
    available in the shared macro regime but not used, or the stored rate drifted
    >5bp from it (stale read). The shared dir may legitimately be absent on
    partial runs, so provenance issues never fail the audit."""
    macro = summary.get("macro_inputs") or {}
    rf, src = macro.get("risk_free_rate"), macro.get("risk_free_source")
    ok = True
    if summary.get("dcf") and rf is not None:
        ok = check("risk_free_in_band", _RF_BAND[0] <= rf <= _RF_BAND[1],
                   f"risk_free_rate={rf}, band={_RF_BAND}")

    # US-rate provenance labels (the live-10Y path). A config_market:{MARKET}
    # source is a deliberate currency-matched static rate, not a US-10Y read, so
    # the live-10Y "available/drift" checks below must NOT apply to it.
    us_rate_src = src in ("DGS10", "^TNX", "shared_csv", "config_fallback")
    regime = load_json(ws.resolve().parent / "shared" / "market_context" / date
                       / "indicators" / "macro_regime.json")
    live = ((regime or {}).get("rates") or {}).get("us10y") or {}
    live_ok = live.get("value") is not None and live.get("data_quality") != "missing"
    if live_ok and src == "config_fallback":
        result["warnings"].append(
            f"live 10Y {live['value']} available in macro_regime but valuation used "
            f"config_fallback rf={rf}")
    elif live_ok and us_rate_src and src != "config_fallback" and rf is not None \
            and abs(rf - live["value"]) > _RF_STALE_TOL:
        result["warnings"].append(
            f"risk_free_rate {rf} ({src}) drifted >5bp from live regime value "
            f"{live['value']} (stale read)")

    # currency mismatch: a non-US/non-USD name should NOT discount CNY/HKD/etc.
    # cash flows with a US risk-free (expected source: config_market:{MARKET}).
    meta = summary.get("meta") or {}
    mp = (meta.get("market_profile") or "US").upper()
    cur = (meta.get("currency") or "USD").upper()
    if (mp != "US" or cur != "USD") and rf is not None and us_rate_src:
        result["warnings"].append(
            f"market_profile={mp}/currency={cur} but risk_free_source={src} is a US "
            f"rate (cash flows likely non-USD — expected config_market:{mp})")
    return ok


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

    # 2b. fair-value range ordering low <= base <= high (the published band must be
    # coherent — a blended base must sit inside its own low/high bounds)
    frng = summary.get("fair_value_range") or {}
    f_low, f_base, f_high = frng.get("low"), frng.get("base"), frng.get("high")
    if None not in (f_low, f_base, f_high):
        hard_ok &= check("fair_value_range_ordered", f_low <= f_base <= f_high,
                         f"low={f_low}, base={f_base}, high={f_high}")

    # 3. sensitivity center cell == base intrinsic value
    base_iv = summary.get("intrinsic_value_base")
    if base_iv is not None and sens_path.exists():
        center = _center_cell(sens_path)
        if center is not None:
            hard_ok &= check("sensitivity_center_eq_base",
                             abs(center - base_iv) <= max(_ABS_TOL, abs(base_iv) * _REL_TOL),
                             f"center={center}, base={base_iv}")

    # 4. margin of safety == (fair_value - price) / price
    mos = summary.get("margin_of_safety")
    price = summary.get("current_price")
    fair_value = summary.get("fair_value")
    mos_anchor = fair_value if fair_value is not None else base_iv
    if mos is not None and mos_anchor is not None and price:
        recomputed = (mos_anchor - price) / price
        hard_ok &= check("margin_of_safety_recompute",
                         abs(recomputed - mos) <= _MOS_TOL,
                         f"stored={mos}, recomputed={recomputed:.4f}")

    # 5. comps blended implied value == mean of earnings/cash-flow components
    imp = summary.get("comps_implied_value") or {}
    comps = [imp.get("by_pe"), imp.get("by_ev_ebitda")]
    present = [v for v in comps if isinstance(v, (int, float)) and v > 0]
    if imp.get("blended") is not None and present:
        mean = sum(present) / len(present)
        hard_ok &= check("comps_blended_is_mean",
                         abs(mean - imp["blended"]) <= max(_BLEND_TOL, 0.005 * mean),
                         f"blended={imp['blended']}, mean={mean:.4f}")

    # 6. selected fair value matches included candidate or weighted blend
    candidates = summary.get("method_candidates") or []
    active = [c for c in candidates
              if c.get("included") and isinstance(c.get("value"), (int, float))
              and c.get("value") > 0]
    if fair_value is not None and active:
        total_w = sum(c.get("weight") or 0.0 for c in active)
        if summary.get("valuation_method") == "blended" and total_w > 0:
            recomputed = sum(c["value"] * c["weight"] for c in active) / total_w
            hard_ok &= check("fair_value_blend_recompute",
                             abs(recomputed - fair_value) <= max(_ABS_TOL, 0.005 * recomputed),
                             f"stored={fair_value}, recomputed={recomputed:.4f}")
        else:
            vals = [c["value"] for c in active]
            hard_ok &= check("fair_value_matches_candidate",
                             any(abs(v - fair_value) <= _ABS_TOL for v in vals),
                             f"fair_value={fair_value}, candidates={vals}")

    # 7. live risk-free rate sanity + provenance
    hard_ok &= _check_risk_free(summary, ws, date, check, result)

    # ── warnings (do not fail) ────────────────────────────────────────────────
    if dcf and dcf.get("tv_fraction_in_band") is False:
        base_tv = (dcf.get("scenarios", {}).get("base", {}) or {}).get("tv_fraction")
        if base_tv is not None and base_tv > 0.70:
            note = "above 70% — model leans on terminal value"
        else:
            note = "below 50% — explicit phase carries the value (terminal-light)"
        result["warnings"].append(
            f"terminal value fraction {base_tv} outside 50–70% band ({note})")
    if summary.get("confidence") == "low":
        result["warnings"].append(
            f"low confidence; inputs_missing={summary.get('inputs_missing')}")

    # Confidence-label lock (warning only): the stated confidence must not exceed
    # what the included candidates support. A low-confidence DCF — single-method
    # or in a blend — cannot read "high". This is a label drift, not a math error,
    # so it warns rather than failing the math audit.
    method = summary.get("valuation_method")
    stated = summary.get("confidence")
    if method in ("dcf", "comps_earnings", "blended") and stated in _CONF_ORD:
        ceiling = _component_confidence(candidates)
        if ceiling is not None and _CONF_ORD[stated] > _CONF_ORD[ceiling]:
            result["warnings"].append(
                f"stated confidence '{stated}' exceeds component-supported '{ceiling}' "
                f"for method '{method}'")

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
