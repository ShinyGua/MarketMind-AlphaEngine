#!/usr/bin/env python3
"""Export stage — the one code path both drivers use to release a run.

Re-checks the decision with eval/pre_release_gate.release_check (never the
verdict file alone, which the LLM steps can write). Passed: the latest draft
becomes final/{date}/{basename}.md and the combined {basename}.json carries the
decision from the same verified bytes. Anything else: final/{date}/BLOCKED.md
lists why, and no report is exported.

The draft is written before the panel decides, so its header "Decision" field
is the thesis map's view, not the decision. Export rewrites that one field
from the gated decision and, when the draft's view was a different label, says
so under the header; a draft with no such field gets one under its title. The
narrative itself stays the writer's.

Usage:
    .venv/bin/python3 scripts/export_release.py <workspace> <date>

Exit: 0 exported, 2 blocked, 1 no draft to export or usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "eval"))
from pre_release_gate import release_check  # noqa: E402
from shared import contracts  # noqa: E402

# "**Decision:** <value>" up to the next field separator or the end of its line,
# in either report language
_DECISION_FIELD = re.compile(r"(\*\*(?:Decision:|决策：)\*\*[ \t]*)([^|｜\n]*?)([ \t]*(?:[|｜]|$))",
                             re.MULTILINE)
FIELD = {"en": "**Decision:**", "ch": "**决策：**"}
_LABEL = re.compile(r"\b(BUY|HOLD|SELL)\b")
HEADER = {"en": "{label} (risk overlay {overlay}, confidence {confidence})",
          "ch": "{label}（风险叠加 {overlay}，信心 {confidence}）"}
DRAFT_NOTE = {"en": "> Note: this report was drafted under a {draft} view; the panel's final "
                    "decision, which passed the pre-release gate, is {label}.",
              "ch": "> 注：本报告撰写时的观点为 {draft}；决策小组的最终决策为 {label}，"
                    "已通过发布前检查。"}
UNDECIDED_NOTE = {"en": "> Note: this report was drafted before the panel decided; its final "
                        "decision, which passed the pre-release gate, is {label}.",
                  "ch": "> 注：本报告撰写于决策小组表决之前；最终决策为 {label}，已通过发布前检查。"}


def _config(ws: Path) -> dict:
    try:
        cfg = json.loads((ws / "resolved_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def latest_draft(ws: Path, date: str) -> Path | None:
    d = ws / "drafts" / date
    drafts = sorted(d.glob("*.md"), key=lambda q: q.stat().st_mtime) if d.is_dir() else []
    return drafts[-1] if drafts else None


def with_gated_header(text: str, decision: dict, lang: str) -> str:
    """Put the gated decision in the header's Decision field (first one only)."""
    lang = lang if lang in HEADER else "en"
    label = str(decision.get("decision") or "").upper()
    conf = decision.get("confidence")
    value = HEADER[lang].format(label=label, overlay=decision.get("risk_overlay") or "none",
                                confidence=f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a")
    m = _DECISION_FIELD.search(text)
    if m is None:   # no header field: state the decision under the title (or first)
        lines = text.split("\n")
        at = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("# ")), 0)
        return "\n".join(lines[:at] + [f"{FIELD[lang]} {value}", ""] + lines[at:])
    drafted = _LABEL.search(m.group(2))
    out = text[:m.start(2)] + value + text[m.end(2):]
    if drafted is None or drafted.group(1) != label:
        end = out.find("\n", m.start(2))
        end = len(out) if end < 0 else end
        note = (DRAFT_NOTE[lang].format(draft=drafted.group(1), label=label) if drafted
                else UNDECIDED_NOTE[lang].format(label=label))
        out = out[:end] + "\n\n" + note + out[end:]
    return out


def write_blocked(ws: Path, date: str, ticker: str, chk: dict) -> Path:
    lines = [f"# {ticker} {date} — release blocked", "",
             "The decision did not pass the pre-release gate, so no report was exported.", ""]
    for e in chk["errors"] or [{"check": chk["status"], "message": "no passing gate result "
                                "for the decision as it stands"}]:
        lines.append(f"- `{e.get('check')}`: {e.get('message')}")
    out = contracts.blocked_report_path(ws, date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def export(ws: Path, date: str, run_mode: str | None = None, lang: str | None = None) -> dict:
    """{status: exported | blocked | no_draft, path}."""
    cfg = _config(ws)
    run_mode = run_mode or cfg.get("run_mode", "daily")
    lang = lang or cfg.get("language", "en")
    ticker = (cfg.get("company") or {}).get("ticker") or ws.name
    chk = release_check(ws, date)
    if chk["status"] != "passed":
        return {"status": "blocked", "path": str(write_blocked(ws, date, ticker, chk)),
                "gate": chk["status"]}
    draft = latest_draft(ws, date)
    if draft is None:
        return {"status": "no_draft", "path": None}
    final_md = contracts.final_report_path(ws, date, run_mode)
    final_md.parent.mkdir(parents=True, exist_ok=True)
    final_md.write_text(with_gated_header(draft.read_text(encoding="utf-8"), chk["decision"], lang),
                        encoding="utf-8")
    contracts.final_report_json_path(ws, date, run_mode).write_text(
        json.dumps({"report_markdown": final_md.name, "draft_source": draft.name,
                    "decision": chk["decision"], "decision_sha256": chk["sha256"],
                    # unsealed: re-validated now, not a record of what was knowable at issuance
                    "sealed": chk["sealed"],
                    "exported_at": datetime.now(timezone.utc).isoformat()},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "exported", "path": str(final_md)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a released run (or write BLOCKED.md)")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("date")
    args = ap.parse_args()
    if not args.workspace.is_dir():
        print(f"export_release: no workspace {args.workspace}", file=sys.stderr)
        return 1
    result = export(args.workspace, args.date)
    print(f"export_release: {result['status']} {result.get('path') or ''}".rstrip())
    return {"exported": 0, "blocked": 2}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
