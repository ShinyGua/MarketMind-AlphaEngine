#!/usr/bin/env python3
"""Discussion convergence grader: decide whether the discussion panel iterates or exits.

The discuss stage runs a multi-round panel: each analyst role files a structured
view {stance, conviction, ...} per round. This grader turns "did the panel
converge on a directional thesis?" into a deterministic, reproducible signal so
the orchestrator/driver loop can exit early on agreement and ALWAYS auto-exit at
the round cap (it never relies on an LLM to decide when to stop).

It is the discussion-stage analog of eval/graders/panel_convergence_grader.py
(the decision panel): stances are bullish|neutral|bearish instead of
BUY|HOLD|SELL votes.

CLI:
    discussion_convergence_grader.py <workspace> <date> <round>

Reads views from discussion/{date}/panel/round_{N}/*_view.json and writes
discussion/{date}/panel/convergence_round_{N}.json.

Convergence:
    conviction_weighted_agreement = Σ conviction(majority stance) / Σ conviction(all)
    stance_stability              = fraction of roles whose stance == their round N-1 stance
                                    (null at round 1 — nothing to compare to)
    convergence_score             = 0.6*cwa + 0.4*stability   (= cwa at round 1)

Exit (deterministic):
    exit = round >= max_rounds
           OR (round >= min_rounds AND convergence_score >= convergence_threshold)

Self-degrading: fewer than 2 readable views → exit with reason
"insufficient_views" (never stalls the pipeline). Thresholds come from
resolved_config.json -> discussion.panel (with safe defaults).
"""
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import (  # noqa: E402
    discussion_panel_round_dir, discussion_convergence_path,
)

DEFAULTS = {
    "min_rounds": 1,
    "max_rounds": 3,
    "convergence_threshold": 0.70,
}

STANCES = ("bullish", "neutral", "bearish")


def _load_thresholds(ws: Path) -> dict:
    out = dict(DEFAULTS)
    try:
        cfg = json.loads((ws / "resolved_config.json").read_text(encoding="utf-8"))
        panel = (cfg.get("discussion", {}) or {}).get("panel", {}) or {}
        for k in out:
            if isinstance(panel.get(k), (int, float)):
                out[k] = panel[k]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    return out


def _read_views(ws: Path, date: str, rnd: int) -> dict:
    """Return {role: view_dict} for a round, skipping unreadable files."""
    d = discussion_panel_round_dir(ws, date, rnd)
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*_view.json")):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        role = v.get("role") or f.name[: -len("_view.json")]
        out[role] = v
    return out


def _norm_stance(s) -> str:
    s = str(s or "").strip().lower()
    return s if s in STANCES else "neutral"


def _norm_conviction(c) -> float:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, c))


def _why(view: dict) -> str:
    """Best one-line reason for a dissent: first core claim, else changed beliefs."""
    claims = view.get("core_claims")
    if isinstance(claims, list) and claims:
        return claims[0]
    return view.get("changed_beliefs") or ""


def grade(workspace: str, date: str, rnd: int) -> dict:
    ws = Path(workspace)
    th = _load_thresholds(ws)
    min_rounds = int(th["min_rounds"])
    max_rounds = int(th["max_rounds"])
    threshold = float(th["convergence_threshold"])

    views = _read_views(ws, date, rnd)
    result = {
        "grader": "discussion_convergence",
        "round": rnd,
        "views": len(views),
        "thresholds": th,
    }

    if len(views) < 2:
        # Not enough signal to judge a debate — exit rather than stall.
        result.update({
            "convergence_score": None,
            "majority_stance": None,
            "agreement_ratio": None,
            "conviction_weighted_agreement": None,
            "stance_stability": None,
            "dissenters": [],
            "exit": True,
            "exit_reason": "insufficient_views",
        })
        return result

    # Tally stances + conviction.
    tally = {s: 0 for s in STANCES}
    conv_by_stance = {s: 0.0 for s in STANCES}
    total_conv = 0.0
    for v in views.values():
        s = _norm_stance(v.get("stance"))
        c = _norm_conviction(v.get("conviction"))
        tally[s] += 1
        conv_by_stance[s] += c
        total_conv += c

    n = len(views)
    # Majority by head-count, tie-broken by conviction mass.
    majority_stance = max(STANCES, key=lambda s: (tally[s], conv_by_stance[s]))
    agreement_ratio = tally[majority_stance] / n
    cwa = (conv_by_stance[majority_stance] / total_conv) if total_conv > 0 else agreement_ratio

    # Stance stability vs previous round.
    stance_stability = None
    if rnd > 1:
        prev = _read_views(ws, date, rnd - 1)
        shared = [r for r in views if r in prev]
        if shared:
            unchanged = sum(
                1 for r in shared
                if _norm_stance(views[r].get("stance")) == _norm_stance(prev[r].get("stance"))
            )
            stance_stability = unchanged / len(shared)

    if stance_stability is None:
        convergence_score = cwa
    else:
        convergence_score = 0.6 * cwa + 0.4 * stance_stability

    dissenters = [
        {"role": r, "stance": _norm_stance(v.get("stance")), "why": _why(v)}
        for r, v in views.items()
        if _norm_stance(v.get("stance")) != majority_stance
    ]

    converged = convergence_score >= threshold and rnd >= min_rounds
    at_cap = rnd >= max_rounds
    if converged:
        exit_, reason = True, "converged"
    elif at_cap:
        exit_, reason = True, "max_rounds"
    else:
        exit_, reason = False, "min_rounds_not_met" if rnd < min_rounds else "not_converged"

    result.update({
        "tally": tally,
        "majority_stance": majority_stance,
        "agreement_ratio": round(agreement_ratio, 4),
        "conviction_weighted_agreement": round(cwa, 4),
        "stance_stability": None if stance_stability is None else round(stance_stability, 4),
        "convergence_score": round(convergence_score, 4),
        "dissenters": dissenters,
        "exit": exit_,
        "exit_reason": reason,
    })
    return result


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <workspace> <date> <round>", file=sys.stderr)
        sys.exit(1)
    workspace, date, rnd = sys.argv[1], sys.argv[2], int(sys.argv[3])
    result = grade(workspace, date, rnd)

    ws = Path(workspace)
    out_path = discussion_convergence_path(ws, date, rnd)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
