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

Anti-conformity guards (deterministic):
    - uncited flips: a role whose stance changed vs round N-1 without citing a
      cause (changed_beliefs + chair answer or evidence ids) carries only half
      its conviction in the cwa — conformity-driven movement must not
      strengthen convergence.
    - tie: labels tied on (head-count, conviction mass) can never converge;
      surfaced as `tie_between`.
    - conviction collapse: total conviction dropping below
      `conviction_collapse_ratio` of the prior round suppresses an early
      "converged" exit (everyone agreeing at much lower conviction is fake
      consensus, not agreement).
    - stall: score below threshold but moving less than `stall_epsilon` vs the
      prior round exits with reason "stalled" and `unresolved_dissent: true`
      instead of grinding the panel into artificial agreement.
    - unanimity challenge (devil's advocate): round-1 PERFECT unanimity is
      consensus that was never tested. When `devils_advocate_round` is on and
      the panel is not at the cap, the converged exit is suppressed
      (reason "unanimity_challenge") and the lowest-conviction role is named
      `devils_advocate` — next round they steelman the case against the
      consensus (keeping their honest stance). Round-2 confirmation then
      counts as tested unanimity (rounds_run = 2 lifts the decision_risk
      round-1-unanimity cap by design).

Exit (deterministic):
    exit = round >= max_rounds
           OR (round >= min_rounds AND convergence_score >= convergence_threshold
               AND no tie AND no conviction collapse
               AND NOT (round-1 unanimity with devils_advocate_round on))
           OR stalled

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
    "conviction_collapse_ratio": 0.75,
    "stall_epsilon": 0.05,
    # bool passes _load_thresholds' (int, float) check (bool is a subclass of int)
    "devils_advocate_round": True,
}

UNCITED_FLIP_WEIGHT = 0.5  # conviction haircut for stance flips without a cited cause

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


def _flip_cited(view: dict) -> bool:
    """A stance flip counts as cited when the panelist names what changed their
    mind: a changed_beliefs note plus either an answer to the chair's flagged
    dissent or explicit evidence ids."""
    changed = str(view.get("changed_beliefs") or "").strip().lower()
    if not changed or changed == "none":
        return False
    answers = str(view.get("answers_to_prior_chair_notes") or "").strip()
    ev = view.get("evidence_ids")
    return bool(answers) or (isinstance(ev, list) and len(ev) > 0)


def _prev_convergence_score(ws: Path, date: str, rnd: int):
    """convergence_score from round N-1's grader output, or None."""
    if rnd <= 1:
        return None
    try:
        prev = json.loads(
            discussion_convergence_path(ws, date, rnd - 1).read_text(encoding="utf-8"))
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
            "tie_between": None,
            "conviction_retention": None,
            "conviction_collapse": False,
            "uncited_flips": [],
            "unresolved_dissent": False,
            "devils_advocate": None,
            "at_max_rounds": rnd >= max_rounds,
            "dissenters": [],
            "exit": True,
            "exit_reason": "insufficient_views",
        })
        return result

    # Previous-round views: stability, flip detection, conviction retention.
    prev = _read_views(ws, date, rnd - 1) if rnd > 1 else {}
    uncited_flips = sorted(
        r for r in views
        if r in prev
        and _norm_stance(views[r].get("stance")) != _norm_stance(prev[r].get("stance"))
        and not _flip_cited(views[r])
    )

    # Tally stances + conviction. Uncited flips carry only half their conviction
    # in the weighted agreement; raw totals still drive the retention check.
    tally = {s: 0 for s in STANCES}
    conv_by_stance = {s: 0.0 for s in STANCES}
    total_conv = 0.0
    total_conv_raw = 0.0
    for r, v in views.items():
        s = _norm_stance(v.get("stance"))
        c = _norm_conviction(v.get("conviction"))
        c_eff = c * UNCITED_FLIP_WEIGHT if r in uncited_flips else c
        tally[s] += 1
        conv_by_stance[s] += c_eff
        total_conv += c_eff
        total_conv_raw += c

    n = len(views)
    # Majority by head-count, tie-broken by conviction mass.
    majority_stance = max(STANCES, key=lambda s: (tally[s], conv_by_stance[s]))
    top_key = (tally[majority_stance], conv_by_stance[majority_stance])
    tied = [s for s in STANCES if tally[s] > 0 and (tally[s], conv_by_stance[s]) == top_key]
    tie_between = tied if len(tied) > 1 else None
    agreement_ratio = tally[majority_stance] / n
    cwa = (conv_by_stance[majority_stance] / total_conv) if total_conv > 0 else agreement_ratio

    # Conviction retention vs previous round (raw, pre-haircut): unanimous
    # stances reached by everyone deflating their conviction are fake consensus.
    conviction_retention = None
    if prev:
        prev_total = sum(_norm_conviction(v.get("conviction")) for v in prev.values())
        if prev_total > 0:
            conviction_retention = total_conv_raw / prev_total
    collapse_ratio = float(th["conviction_collapse_ratio"])
    conviction_collapse = (conviction_retention is not None
                           and conviction_retention < collapse_ratio)

    # Stance stability vs previous round.
    stance_stability = None
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

    at_cap = rnd >= max_rounds

    # Round-1 perfect unanimity is consensus that was never tested: name a
    # devil's advocate (lowest conviction — least invested, best positioned to
    # steelman the opposing case) and hold the panel for one challenge round.
    devils_advocate = None
    if (rnd == 1 and not at_cap and bool(th["devils_advocate_round"])
            and agreement_ratio == 1.0):
        devils_advocate = min(
            views, key=lambda r: (_norm_conviction(views[r].get("conviction")), r))

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
        "majority_stance": majority_stance,
        "agreement_ratio": round(agreement_ratio, 4),
        "conviction_weighted_agreement": round(cwa, 4),
        "stance_stability": None if stance_stability is None else round(stance_stability, 4),
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
    out_path = discussion_convergence_path(ws, date, rnd)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
