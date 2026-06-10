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

Anti-conformity guards (deterministic, mirrors discussion_convergence_grader):
    - uncited flips: a role whose vote changed vs round N-1 without citing a
      cause (changed_from_prev + dissent answer or ev_ reference) carries only
      half its conviction in the cwa.
    - tie: labels tied on (head-count, conviction mass) can never converge;
      surfaced as `tie_between`.
    - conviction collapse: total conviction dropping below
      `conviction_collapse_ratio` of the prior round suppresses an early
      "converged" exit.
    - stall: score below threshold but moving less than `stall_epsilon` vs the
      prior round exits with reason "stalled" and `unresolved_dissent: true`.
    - unanimity challenge (devil's advocate): round-1 PERFECT unanimity is
      consensus that was never tested. When `devils_advocate_round` is on and
      the panel is not at the cap, the converged exit is suppressed
      (reason "unanimity_challenge") and the lowest-conviction role is named
      `devils_advocate` — next round they steelman the case against the
      consensus (keeping their honest vote).

Exit (deterministic):
    exit = round >= max_rounds
           OR (round >= min_rounds AND convergence_score >= convergence_threshold
               AND no tie AND no conviction collapse
               AND NOT (round-1 unanimity with devils_advocate_round on))
           OR stalled

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
    "conviction_collapse_ratio": 0.75,
    "stall_epsilon": 0.05,
    # bool passes _load_thresholds' (int, float) check (bool is a subclass of int)
    "devils_advocate_round": True,
}

UNCITED_FLIP_WEIGHT = 0.5  # conviction haircut for vote flips without a cited cause

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


def _flip_cited(ballot: dict) -> bool:
    """A vote flip counts as cited when the panelist names what changed their
    mind: a changed_from_prev note plus either an answer to the chair's flagged
    dissent or an explicit evidence reference in the rationale."""
    changed = str(ballot.get("changed_from_prev") or "").strip().lower()
    if not changed or changed == "none":
        return False
    responds = str(ballot.get("responds_to_dissent") or "").strip()
    rationale = str(ballot.get("rationale") or "")
    return bool(responds) or "ev_" in rationale


def _prev_convergence_score(ws: Path, date: str, rnd: int):
    """convergence_score from round N-1's grader output, or None."""
    if rnd <= 1:
        return None
    try:
        prev = json.loads(
            panel_convergence_path(ws, date, rnd - 1).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    score = prev.get("convergence_score")
    return score if isinstance(score, (int, float)) else None


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
            "tie_between": None,
            "conviction_retention": None,
            "conviction_collapse": False,
            "uncited_flips": [],
            "unresolved_dissent": False,
            "devils_advocate": None,
            "at_max_rounds": rnd >= max_rounds,
            "dissenters": [],
            "exit": True,
            "exit_reason": "insufficient_ballots",
        })
        return result

    # Previous-round ballots: stability, flip detection, conviction retention.
    prev = _read_ballots(ws, date, rnd - 1) if rnd > 1 else {}
    uncited_flips = sorted(
        r for r in ballots
        if r in prev
        and _norm_vote(ballots[r].get("vote")) != _norm_vote(prev[r].get("vote"))
        and not _flip_cited(ballots[r])
    )

    # Tally votes + conviction. Uncited flips carry only half their conviction
    # in the weighted agreement; raw totals still drive the retention check.
    tally = {v: 0 for v in VOTES}
    conv_by_vote = {v: 0.0 for v in VOTES}
    total_conv = 0.0
    total_conv_raw = 0.0
    for r, b in ballots.items():
        v = _norm_vote(b.get("vote"))
        c = _norm_conviction(b.get("conviction"))
        c_eff = c * UNCITED_FLIP_WEIGHT if r in uncited_flips else c
        tally[v] += 1
        conv_by_vote[v] += c_eff
        total_conv += c_eff
        total_conv_raw += c

    n = len(ballots)
    # Majority by head-count, tie-broken by conviction mass.
    majority_vote = max(VOTES, key=lambda v: (tally[v], conv_by_vote[v]))
    top_key = (tally[majority_vote], conv_by_vote[majority_vote])
    tied = [v for v in VOTES if tally[v] > 0 and (tally[v], conv_by_vote[v]) == top_key]
    tie_between = tied if len(tied) > 1 else None
    agreement_ratio = tally[majority_vote] / n
    cwa = (conv_by_vote[majority_vote] / total_conv) if total_conv > 0 else agreement_ratio

    # Conviction retention vs previous round (raw, pre-haircut): unanimous
    # votes reached by everyone deflating their conviction are fake consensus.
    conviction_retention = None
    if prev:
        prev_total = sum(_norm_conviction(b.get("conviction")) for b in prev.values())
        if prev_total > 0:
            conviction_retention = total_conv_raw / prev_total
    collapse_ratio = float(th["conviction_collapse_ratio"])
    conviction_collapse = (conviction_retention is not None
                           and conviction_retention < collapse_ratio)

    # Vote stability vs previous round.
    vote_stability = None
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

    at_cap = rnd >= max_rounds

    # Round-1 perfect unanimity is consensus that was never tested: name a
    # devil's advocate (lowest conviction — least invested, best positioned to
    # steelman the opposing case) and hold the panel for one challenge round.
    devils_advocate = None
    if (rnd == 1 and not at_cap and bool(th["devils_advocate_round"])
            and agreement_ratio == 1.0):
        devils_advocate = min(
            ballots, key=lambda r: (_norm_conviction(ballots[r].get("conviction")), r))

    raw_converged = convergence_score >= threshold and rnd >= min_rounds
    suppressed = None
    if raw_converged and tie_between:
        suppressed = "tie_unresolved"
    elif raw_converged and conviction_collapse:
        suppressed = "conviction_collapse"
    elif raw_converged and devils_advocate is not None:
        suppressed = "unanimity_challenge"
    converged = raw_converged and suppressed is None

    # Stalled: below threshold and barely moving vs the prior round's score.
    prev_score = _prev_convergence_score(ws, date, rnd)
    stalled = (not converged and rnd > min_rounds and prev_score is not None
               and convergence_score < threshold
               and abs(convergence_score - prev_score) < float(th["stall_epsilon"]))

    if converged:
        exit_, reason = True, "converged"
    elif at_cap:
        exit_, reason = True, "max_rounds"
    elif stalled:
        exit_, reason = True, "stalled"
    elif suppressed:
        exit_, reason = False, suppressed
    else:
        exit_, reason = False, "min_rounds_not_met" if rnd < min_rounds else "not_converged"

    result.update({
        "tally": tally,
        "majority_vote": majority_vote,
        "agreement_ratio": round(agreement_ratio, 4),
        "conviction_weighted_agreement": round(cwa, 4),
        "vote_stability": None if vote_stability is None else round(vote_stability, 4),
        "convergence_score": round(convergence_score, 4),
        "tie_between": tie_between,
        "conviction_retention": (None if conviction_retention is None
                                 else round(conviction_retention, 4)),
        "conviction_collapse": conviction_collapse,
        "uncited_flips": uncited_flips,
        "unresolved_dissent": stalled,
        "devils_advocate": devils_advocate,
        "at_max_rounds": at_cap,
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
