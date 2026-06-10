#!/usr/bin/env python3
"""Depth grader: flag shallow pipeline output (thin memos / stub report / scant evidence).

A run can be factually accurate yet far too thin — analyst memos compressed to a
few sentences, report sections stubbed, very few evidence cards. The LLM reviewer
does not reliably catch this (it scored a 5-line-memo run 8.4-9.2). This grader
makes "shallow" a deterministic signal so the orchestrator can redo a thin stage
and the release gate can flag it.

CLI:
    depth_grader.py <workspace> <date> [--memos-only | --report-only]

Modes:
    (default)      memos + report + evidence
    --memos-only   memos + evidence   (use before discuss_synthesis)
    --report-only  report             (use at the review stage)

Self-degrading: a missing input makes that check `skipped`, never an error.
Thresholds come from resolved_config.json -> review.depth (with safe defaults).
"""
import sys
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import detect_final_report, grader_result_path, evidence_digest_path

DEFAULTS = {
    "min_memo_chars": 1200,      # per analyst memo (TSLA thin ~300, NVTS deep ~6-10k)
    "min_section_chars": 300,    # per major report section (flags stubs)
    "min_evidence_cards": 12,    # total evidence cards collected
    "min_exec_bullets": 3,       # Executive Summary bullet count
}


def _load_thresholds(ws: Path) -> dict:
    cfg_path = ws / "resolved_config.json"
    out = dict(DEFAULTS)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        depth = (cfg.get("review", {}) or {}).get("depth", {}) or {}
        for k in out:
            if isinstance(depth.get(k), (int, float)):
                out[k] = depth[k]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    return out


def _check_memos(ws: Path, date: str, th: dict) -> dict:
    memo_dir = ws / "discussion" / date / "analyst_memos"
    files = sorted(memo_dir.glob("*.md")) if memo_dir.is_dir() else []
    if not files:
        return {"status": "skipped", "reason": "no analyst memos found"}
    thin = []
    sizes = {}
    for f in files:
        n = len(f.read_text(encoding="utf-8").strip())
        sizes[f.name] = n
        if n < th["min_memo_chars"]:
            thin.append({"memo": f.name, "chars": n, "min": th["min_memo_chars"]})
    return {
        "status": "pass" if not thin else "fail",
        "memos": len(files),
        "thin_memos": thin,
        "sizes": sizes,
    }


def _check_evidence(ws: Path, date: str, th: dict) -> dict:
    digest = evidence_digest_path(ws, date)
    count = None
    if digest.exists():
        try:
            count = len(json.loads(digest.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            count = None
    if count is None:
        cards_dir = ws / "normalized" / date / "evidence_cards"
        if cards_dir.is_dir():
            count = len(list(cards_dir.glob("*.json")))
    if count is None:
        return {"status": "skipped", "reason": "no evidence digest or cards"}
    ok = count >= th["min_evidence_cards"]
    return {"status": "pass" if ok else "fail",
            "evidence_cards": count, "min": th["min_evidence_cards"]}


def _check_report(ws: Path, date: str, th: dict) -> dict:
    report = detect_final_report(ws, date)
    if report is None or not report.exists():
        return {"status": "skipped", "reason": "no final report"}
    text = report.read_text(encoding="utf-8")
    # split into "## Section" blocks
    parts = re.split(r"(?m)^## ", text)
    stub = []
    exec_bullets = None
    for block in parts[1:]:
        lines = block.splitlines()
        # A bare "## " with no heading text (e.g. trailing the report) must not
        # crash the grader — it is itself a stub section.
        head = lines[0].strip() if lines and lines[0].strip() else "(empty)"
        body_len = len(block.strip())
        if body_len < th["min_section_chars"]:
            stub.append({"section": head, "chars": body_len, "min": th["min_section_chars"]})
        if head.lower().startswith("executive summary"):
            exec_bullets = len(re.findall(r"(?m)^\s*[-*] ", block))
    bullets_ok = exec_bullets is None or exec_bullets >= th["min_exec_bullets"]
    status = "pass" if (not stub and bullets_ok) else "fail"
    res = {"status": status, "sections": len(parts) - 1, "stub_sections": stub}
    if exec_bullets is not None:
        res["exec_summary_bullets"] = exec_bullets
        res["min_exec_bullets"] = th["min_exec_bullets"]
    return res


def grade(workspace: str, date: str, mode: str = "full") -> dict:
    ws = Path(workspace)
    th = _load_thresholds(ws)
    checks = {}
    if mode in ("full", "memos-only"):
        checks["memos"] = _check_memos(ws, date, th)
        checks["evidence"] = _check_evidence(ws, date, th)
    if mode in ("full", "report-only"):
        checks["report"] = _check_report(ws, date, th)

    statuses = [c["status"] for c in checks.values()]
    # pass unless any check explicitly failed; all-skipped -> pass (nothing to judge)
    overall = not any(s == "fail" for s in statuses)
    thin_artifacts = []
    if checks.get("memos", {}).get("thin_memos"):
        thin_artifacts += [m["memo"] for m in checks["memos"]["thin_memos"]]
    if checks.get("report", {}).get("stub_sections"):
        thin_artifacts += [s["section"] for s in checks["report"]["stub_sections"]]

    return {
        "grader": "depth",
        "mode": mode,
        "pass": overall,
        "thresholds": th,
        "checks": checks,
        "thin_artifacts": thin_artifacts,
        "errors": [],
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 2:
        print(f"Usage: {sys.argv[0]} <workspace> <date> [--memos-only|--report-only]",
              file=sys.stderr)
        sys.exit(1)
    workspace, date = args[0], args[1]
    mode = "full"
    if "--memos-only" in flags:
        mode = "memos-only"
    elif "--report-only" in flags:
        mode = "report-only"

    result = grade(workspace, date, mode)

    ws = Path(workspace)
    out_path = grader_result_path(ws, date, "depth")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
