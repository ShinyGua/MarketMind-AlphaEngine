#!/usr/bin/env python3
"""Panel convergence grader: decide whether the decision panel iterates or exits.

The decide stage can run a multi-round panel: each analyst role casts a ballot
{vote, conviction, risk_overlay} per round. This grader turns "did the panel
converge?" into a deterministic, reproducible signal so the orchestrator/driver
loop can exit early on agreement and ALWAYS auto-exit at the round cap (it never
relies on an LLM to decide when to stop).

CLI:
    panel_convergence_grader.py <workspace> <date> <round>

Reads ballots from decision/{date}/panel/round_{N}/*_ballot.json and writes
decision/{date}/panel/convergence_round_{N}.json.

Convergence:
    conviction_weighted_agreement = Σ conviction(majority voters) / Σ conviction(all)
    vote_stability                = fraction of roles whose vote == their round N-1 vote
                                    (null at round 1 — nothing to compare to)
    convergence_score             = 0.6*cwa + 0.4*stability   (= cwa at round 1)

Exit (deterministic):
    exit = round >= max_rounds
           OR (round >= min_rounds AND convergence_score >= convergence_threshold)

Self-degrading: fewer than 2 readable ballots → exit with reason
"insufficient_ballots" (never stalls the pipeline). Thresholds come from
resolved_config.json -> decision.panel (with safe defaults).
"""
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import (  # noqa: E402
    panel_round_dir, panel_convergence_path,
)

DEFAULTS = {
    "min_rounds": 1,
    "max_rounds": 3,
    "convergence_threshold": 0.70,
}

VOTES = ("BUY", "HOLD", "SELL")


def _load_thresholds(ws: Path) -> dict:
    out = dict(DEFAULTS)
    try:
        cfg = json.loads((ws / "resolved_config.json").read_text(encoding="utf-8"))
        panel = (cfg.get("decision", {}) or {}).get("panel", {}) or {}
        for k in out:
            if isinstance(panel.get(k), (int, float)):
                out[k] = panel[k]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    return out


def _read_ballots(ws: Path, date: str, rnd: int) -> dict:
    """Return {role: ballot_dict} for a round, skipping unreadable files."""
    d = panel_round_dir(ws, date, rnd)
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*_ballot.json")):
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        role = b.get("role") or f.name[: -len("_ballot.json")]
        out[role] = b
    return out


def _norm_vote(v) -> str:
    v = str(v or "").strip().upper()
    return v if v in VOTES else "HOLD"


def _norm_conviction(c) -> float:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, c))


def grade(workspace: str, date: str, rnd: int) -> dict:
    ws = Path(workspace)
    th = _load_thresholds(ws)
    min_rounds = int(th["min_rounds"])
    max_rounds = int(th["max_rounds"])
    threshold = float(th["convergence_threshold"])

    ballots = _read_ballots(ws, date, rnd)
    result = {
        "grader": "panel_convergence",
        "round": rnd,
        "ballots": len(ballots),
        "thresholds": th,
    }

    if len(ballots) < 2:
        # Not enough signal to judge a debate — exit rather than stall.
        result.update({
            "convergence_score": None,
            "majority_vote": None,
            "agreement_ratio": None,
            "conviction_weighted_agreement": None,
            "vote_stability": None,
            "dissenters": [],
            "exit": True,
            "exit_reason": "insufficient_ballots",
        })
        return result

    # Tally votes + conviction.
    tally = {v: 0 for v in VOTES}
    conv_by_vote = {v: 0.0 for v in VOTES}
    total_conv = 0.0
    for b in ballots.values():
        v = _norm_vote(b.get("vote"))
        c = _norm_conviction(b.get("conviction"))
        tally[v] += 1
        conv_by_vote[v] += c
        total_conv += c

    n = len(ballots)
    # Majority by head-count, tie-broken by conviction mass.
    majority_vote = max(VOTES, key=lambda v: (tally[v], conv_by_vote[v]))
    agreement_ratio = tally[majority_vote] / n
    cwa = (conv_by_vote[majority_vote] / total_conv) if total_conv > 0 else agreement_ratio

    # Vote stability vs previous round.
    vote_stability = None
    if rnd > 1:
        prev = _read_ballots(ws, date, rnd - 1)
        shared = [r for r in ballots if r in prev]
        if shared:
            unchanged = sum(
                1 for r in shared
                if _norm_vote(ballots[r].get("vote")) == _norm_vote(prev[r].get("vote"))
            )
            vote_stability = unchanged / len(shared)

    if vote_stability is None:
        convergence_score = cwa
    else:
        convergence_score = 0.6 * cwa + 0.4 * vote_stability

    dissenters = [
        {"role": r, "vote": _norm_vote(b.get("vote")), "why": b.get("top_risk") or b.get("rationale", "")}
        for r, b in ballots.items()
        if _norm_vote(b.get("vote")) != majority_vote
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
        "majority_vote": majority_vote,
        "agreement_ratio": round(agreement_ratio, 4),
        "conviction_weighted_agreement": round(cwa, 4),
        "vote_stability": None if vote_stability is None else round(vote_stability, 4),
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
    out_path = panel_convergence_path(ws, date, rnd)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
