#!/usr/bin/env python3
"""Compute aggregate metrics from eval/run_log.jsonl.

Usage:
    .venv/bin/python3 eval/metrics.py [--ticker AMD] [--last-n 30] [--format json|markdown]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_LOG_PATH = PROJECT_ROOT / "eval" / "run_log.jsonl"

import sys as _sys
_sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import RELEASE_PASSED, RELEASE_WARNING, RELEASE_FAILED


def load_runs(path: Path) -> list[dict]:
    """Load all JSONL entries, skipping malformed lines."""
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _get(entry: dict, *keys, default=None):
    """Return the first present key's value from *entry*."""
    for k in keys:
        if k in entry and entry[k] is not None:
            return entry[k]
    return default


def safe_mean(values: list) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def safe_pct(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def filter_runs(runs: list[dict], ticker: str | None, last_n: int | None) -> list[dict]:
    if ticker:
        runs = [r for r in runs if r.get("ticker", "").upper() == ticker.upper()]
    runs.sort(key=lambda r: r.get("run_date", ""), reverse=True)
    if last_n is not None and last_n > 0:
        runs = runs[:last_n]
    return runs


def pipeline_health(runs: list[dict]) -> dict:
    total = len(runs)

    # Release-status-aware success rates (primary)
    has_release = [r for r in runs if r.get("release_status") is not None]
    strict_ok = sum(1 for r in has_release if r["release_status"] == RELEASE_PASSED)
    operational_ok = sum(
        1 for r in has_release
        if r["release_status"] in (RELEASE_PASSED, RELEASE_WARNING)
    )

    # Legacy fallback for entries without release_status
    legacy = [r for r in runs if r.get("release_status") is None]
    legacy_ok = sum(1 for r in legacy if r.get("success", r.get("all_graders_pass", False)))

    durations = [r["total_duration_s"] for r in runs if r.get("total_duration_s") is not None]
    iters = [r["review_iterations"] for r in runs if r.get("review_iterations") is not None]

    stage_totals: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        for s in r.get("stages", []):
            name, dur = s.get("name"), s.get("duration_s")
            if name and dur is not None:
                stage_totals[name].append(dur)

    return {
        "total_runs": total,
        "strict_success_rate": safe_pct(strict_ok + legacy_ok, total),
        "operational_success_rate": safe_pct(operational_ok + legacy_ok, total),
        "success_rate": safe_pct(strict_ok + legacy_ok, total),  # backward compat
        "avg_duration_s": safe_mean(durations),
        "avg_review_iterations": safe_mean(iters),
        "stage_avg_duration": {k: safe_mean(v) for k, v in sorted(stage_totals.items())},
    }


def quality_trends(runs: list[dict]) -> dict:
    score_accum: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        scores = _get(r, "final_scores", "final_review_scores")
        if isinstance(scores, dict):
            for dim, val in scores.items():
                if isinstance(val, (int, float)):
                    score_accum[dim].append(val)

    iter_vals = [r["review_iterations"] for r in runs if r.get("review_iterations") is not None]
    first_pass = sum(1 for v in iter_vals if v == 1)

    grader_totals: dict[str, int] = defaultdict(int)
    grader_passes: dict[str, int] = defaultdict(int)
    for r in runs:
        graders = _get(r, "grader_results", "graders")
        if not isinstance(graders, dict):
            continue
        for gname, gval in graders.items():
            if isinstance(gval, dict) and "pass" in gval:
                grader_totals[gname] += 1
                if gval["pass"]:
                    grader_passes[gname] += 1

    return {
        "avg_scores": {k: safe_mean(v) for k, v in sorted(score_accum.items())},
        "first_pass_rate": safe_pct(first_pass, len(iter_vals)),
        "grader_pass_rates": {
            k: safe_pct(grader_passes.get(k, 0), grader_totals[k])
            for k in sorted(grader_totals)
        },
    }


def decision_analytics(runs: list[dict]) -> dict:
    dist: dict[str, int] = defaultdict(int)
    conf_by_dec: dict[str, list[float]] = defaultdict(list)
    ticker_map: dict[str, list[dict]] = defaultdict(list)
    all_confs: list[float] = []

    for r in runs:
        dec = _get(r, "final_decision", "decision")
        conf = _get(r, "final_confidence", "confidence")
        ticker = r.get("ticker")

        if dec:
            dec = dec.upper()
            dist[dec] += 1
            if isinstance(conf, (int, float)):
                conf_by_dec[dec].append(conf)
        if isinstance(conf, (int, float)):
            all_confs.append(conf)
        if ticker and dec:
            ticker_map[ticker].append(
                {"date": r.get("run_date"), "decision": dec, "confidence": conf}
            )

    for tk in ticker_map:
        ticker_map[tk].sort(key=lambda x: x.get("date") or "")

    return {
        "decision_distribution": dict(sorted(dist.items())),
        "avg_confidence": safe_mean(all_confs),
        "confidence_by_decision": {k: safe_mean(v) for k, v in sorted(conf_by_dec.items())},
        "ticker_decisions": dict(sorted(ticker_map.items())),
    }


def cost_analytics(runs: list[dict]) -> dict:
    tokens: list[int] = []
    costs: list[float] = []
    for r in runs:
        graders = _get(r, "grader_results", "graders")
        if not isinstance(graders, dict):
            continue
        cost_obj = graders.get("cost")
        if not isinstance(cost_obj, dict):
            continue
        t = cost_obj.get("estimated_total_tokens")
        c = cost_obj.get("estimated_cost_usd")
        if isinstance(t, (int, float)):
            tokens.append(int(t))
        if isinstance(c, (int, float)):
            costs.append(float(c))

    return {
        "avg_tokens_per_run": safe_mean(tokens),
        "avg_cost_per_run": safe_mean(costs),
        "total_cost": round(sum(costs), 4) if costs else 0.0,
    }


def compute_all(runs: list[dict]) -> dict:
    return {
        "pipeline_health": pipeline_health(runs),
        "quality_trends": quality_trends(runs),
        "decision_analytics": decision_analytics(runs),
        "cost_analytics": cost_analytics(runs),
    }


# -- Formatters ---------------------------------------------------------------

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    col_w = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_w[i] = max(col_w[i], len(cell))
    hdr = "| " + " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)) + " |"
    sep = "| " + " | ".join("-" * col_w[i] for i in range(len(headers))) + " |"
    body = "\n".join(
        "| " + " | ".join(cell.ljust(col_w[i]) for i, cell in enumerate(row)) + " |"
        for row in rows
    )
    return f"{hdr}\n{sep}\n{body}"


def format_markdown(m: dict) -> str:
    lines: list[str] = []
    ph, qt, da, ca = (
        m["pipeline_health"], m["quality_trends"],
        m["decision_analytics"], m["cost_analytics"],
    )

    lines.append("## Pipeline Health\n")
    lines.append(_md_table(["Metric", "Value"], [
        ["Total runs", str(ph["total_runs"])],
        ["Strict success rate", f"{ph['strict_success_rate']:.2%}"],
        ["Operational success rate", f"{ph['operational_success_rate']:.2%}"],
        ["Avg duration (s)", f"{ph['avg_duration_s']:.1f}"],
        ["Avg review iterations", f"{ph['avg_review_iterations']:.2f}"],
    ]))
    if ph["stage_avg_duration"]:
        lines.append("\n### Stage Avg Duration\n")
        lines.append(_md_table(["Stage", "Avg (s)"],
                               [[k, f"{v:.1f}"] for k, v in ph["stage_avg_duration"].items()]))

    lines.append("\n## Quality Trends\n")
    qt_rows = [["First-pass rate", f"{qt['first_pass_rate']:.2%}"]]
    qt_rows += [[f"Avg {dim}", f"{val:.2f}"] for dim, val in qt["avg_scores"].items()]
    lines.append(_md_table(["Metric", "Value"], qt_rows))
    if qt["grader_pass_rates"]:
        lines.append("\n### Grader Pass Rates\n")
        lines.append(_md_table(["Grader", "Pass Rate"],
                               [[k, f"{v:.2%}"] for k, v in qt["grader_pass_rates"].items()]))

    lines.append("\n## Decision Analytics\n")
    da_rows = [["Avg confidence", f"{da['avg_confidence']:.4f}"]]
    da_rows += [[f"{d} count", str(c)] for d, c in da["decision_distribution"].items()]
    da_rows += [[f"{d} avg confidence", f"{a:.4f}"] for d, a in da["confidence_by_decision"].items()]
    lines.append(_md_table(["Metric", "Value"], da_rows))

    lines.append("\n## Cost Analytics\n")
    lines.append(_md_table(["Metric", "Value"], [
        ["Avg tokens/run", f"{ca['avg_tokens_per_run']:.0f}"],
        ["Avg cost/run ($)", f"{ca['avg_cost_per_run']:.4f}"],
        ["Total cost ($)", f"{ca['total_cost']:.4f}"],
    ]))

    return "\n".join(lines)


# -- CLI ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compute aggregate metrics from run_log.jsonl")
    parser.add_argument("--ticker", type=str, default=None, help="Filter to a specific ticker")
    parser.add_argument("--last-n", type=int, default=None, help="Use only the last N runs (by date)")
    parser.add_argument("--format", dest="fmt", choices=["json", "markdown"], default="json",
                        help="Output format (default: json)")
    args = parser.parse_args()

    runs = load_runs(RUN_LOG_PATH)
    runs = filter_runs(runs, args.ticker, args.last_n)
    metrics = compute_all(runs)
    print(format_markdown(metrics) if args.fmt == "markdown" else json.dumps(metrics, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
