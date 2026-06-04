#!/usr/bin/env python3
"""Guard against prompt/runtime contract drift (CX2CC review §4.1).

Greps the skill + command tree for known-bad patterns that have caused
silent failures: the wrong NewsAPI env var, undated artifact paths that
contradict the date-stamped contract, the wrong news-config key, and a
stale stage count. Exits non-zero (and prints each violation) if any are
found, so it can run in CI / under pytest.

Usage:
    .venv/bin/python3 scripts/contract_check.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
COMMANDS = ROOT / "plugin" / "commands"

# Undated path fragments that must always be date-stamped when they appear
# under a {workspace}/ reference. Each maps to the correct dated form.
UNDATED_PATTERNS = [
    r"\{workspace\}/raw/prices/",
    r"\{workspace\}/raw/news/",
    r"\{workspace\}/raw/calendar/",
    r"\{workspace\}/normalized/evidence_cards/",
    r"\{workspace\}/quant/relative_strength",
    r"\{workspace\}/quant/technical_indicators",
    r"\{workspace\}/discussion/analyst_memos/",
    r"\{workspace\}/discussion/thesis_map\.json",
    r"\{workspace\}/discussion/debate_summary\.md",
    r"\{workspace\}/discussion/debate/",
    r"shared/market_context/raw/",
]


def _iter_md(base: Path):
    if not base.exists():
        return
    yield from base.rglob("*.md")


def check_newsapi_env() -> list[str]:
    """Runtime (skills + .mcp.json + server) must use NEWSAPI_KEY, not
    NEWSAPI_API_KEY. Docs (README) may use either."""
    violations = []
    targets = list(_iter_md(SKILLS))
    targets += [ROOT / ".mcp.json", ROOT / "mcp" / "market_data_server.py"]
    for f in targets:
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "NEWSAPI_API_KEY" in line:
                violations.append(f"{f.relative_to(ROOT)}:{i}: use NEWSAPI_KEY, not NEWSAPI_API_KEY")
    return violations


def check_undated_paths() -> list[str]:
    violations = []
    for f in _iter_md(SKILLS):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for pat in UNDATED_PATTERNS:
                if re.search(pat, line):
                    violations.append(f"{f.relative_to(ROOT)}:{i}: undated path /{pat}/ — must include /{{date}}/")
    return violations


def check_news_config_key() -> list[str]:
    """News caps live under data_sources.news, not a top-level 'news' key."""
    violations = []
    for f in _iter_md(SKILLS):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'config\["news"\]', line):
                violations.append(f"{f.relative_to(ROOT)}:{i}: use config[\"data_sources\"][\"news\"]")
    return violations


def check_stage_count() -> list[str]:
    """The pipeline has 15 stages; status examples must not say /14."""
    violations = []
    for f in _iter_md(COMMANDS):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b\d+/14\b", line):
                violations.append(f"{f.relative_to(ROOT)}:{i}: stale stage count '/14' — pipeline has 15 stages")
    return violations


def run_checks() -> list[str]:
    violations: list[str] = []
    violations += check_newsapi_env()
    violations += check_undated_paths()
    violations += check_news_config_key()
    violations += check_stage_count()
    return violations


def main() -> int:
    violations = run_checks()
    if violations:
        print("Contract check FAILED — prompt/runtime drift detected:\n")
        for v in violations:
            print(f"  {v}")
        print(f"\n{len(violations)} violation(s).")
        return 1
    print("Contract check passed: no prompt/runtime drift detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
