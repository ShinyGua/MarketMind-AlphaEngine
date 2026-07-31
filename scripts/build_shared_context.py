#!/usr/bin/env python3
"""Build the per-run shared context bundle (shared_context/{date}.json).

Committed extraction of the bundler both drivers previously inlined (the Codex
driver never had it — this closes that parity gap). Bundles everything ALL
downstream agents need: quant, valuation, profile, peers, catalysts, plus the
macro layer:
  - macro_regime: shared market_context/{date}/indicators/macro_regime.json
                  (context, not trigger — never a standalone stance reason)
  - intraday:     quant/{date}/intraday_timing.json (timing-only — frames entry/
                  exit zones, never a vote reason)

  - chips:        quant/{date}/chip_structure.json (DIRECTIONAL — volume/chip
                  structure is first-class stance evidence; histogram stripped
                  to keep the bundle lean)
  - peer_divergence: quant/{date}/peer_divergence.json (per-peer path classes)
  - investor:     profile/investor_profile.json (horizon / edge hypothesis /
                  position state — whose question the pipeline is answering)

Absent inputs are skipped (self-degrading). Always exits 0.

Usage:
    .venv/bin/python3 scripts/build_shared_context.py <workspace> <date>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
from shared import contracts  # noqa: E402


def build(workspace: Path, date: str) -> dict:
    ws = Path(workspace)
    sections = {
        "quant": ws / "quant" / date / "quant_summary.json",
        "valuation": ws / "valuation" / date / "valuation_summary.json",
        "profile": ws / "profile" / "company_profile.json",
        "peers": ws / "profile" / "peer_set.json",
        "macro_regime": contracts.macro_regime_path(ws, date),
        "intraday": contracts.intraday_timing_path(ws, date),
        "chips": contracts.chip_structure_path(ws, date),
        "peer_divergence": contracts.peer_divergence_path(ws, date),
        "investor": contracts.investor_profile_path(ws),
    }
    ctx = {}
    for name, path in sections.items():
        try:
            ctx[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    # the VPVR histogram is chart fodder — too token-heavy for analyst context
    if isinstance(ctx.get("chips"), dict):
        (ctx["chips"].get("chip_distribution") or {}).pop("histogram", None)
    for cp in (ws / "raw" / date / "calendar" / "catalysts.json",
               ws / "raw" / "calendar" / "catalysts.json"):
        try:
            ctx["catalysts"] = json.loads(cp.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue

    out = contracts.shared_context_path(ws, date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ctx, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"build_shared_context: wrote {out} ({len(ctx)} sections: {', '.join(ctx)})")
    return ctx


def main():
    if len(sys.argv) < 3:
        print("usage: build_shared_context.py <workspace> <date>")
        return
    try:
        build(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:  # bundling is non-critical — never fail the pipeline
        print(f"build_shared_context: ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
