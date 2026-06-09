"""Contract checks for the discussion panel loop.

Guards the doc/skill surface after selective debate was replaced by the panel:
- the analyst memo skills no longer instruct selective cross-critique
- the moderator exposes a `tally` mode and no longer a `scan` mode
- the panelist's output path matches what the convergence grader globs
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"

ANALYST_SKILLS = ("mm-market-analyst", "mm-company-analyst", "mm-risk-analyst",
                  "mm-valuation-analyst", "mm-technical-analyst", "mm-catalyst-analyst")
BANNED_IN_ANALYSTS = ("Cross-Critique", "debate round_N", "### Mode B",
                      "selective debate", "_on_{target}")


def test_analyst_skills_drop_selective_debate():
    for name in ANALYST_SKILLS:
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for phrase in BANNED_IN_ANALYSTS:
            assert phrase not in text, f"{name} still instructs selective debate: {phrase!r}"


def test_moderator_has_tally_not_scan():
    text = (SKILLS / "mm-discussion-moderator" / "SKILL.md").read_text(encoding="utf-8")
    assert "tally" in text, "moderator missing the per-round tally mode"
    assert "debate_assignments.json" not in text, "moderator still writes debate_assignments.json"
    assert "Debate Assignment Scan" not in text, "moderator still documents the scan mode"


def test_panelist_output_path_matches_grader_glob():
    panelist = (SKILLS / "mm-discussion-panelist" / "SKILL.md").read_text(encoding="utf-8")
    # panelist writes one view per role per round under the panel round dir
    assert "panel/round_{N}/{role}_view.json" in panelist, \
        "panelist output path not documented as expected"
    grader = (ROOT / "eval" / "graders" / "discussion_convergence_grader.py").read_text(encoding="utf-8")
    assert '"*_view.json"' in grader or "'*_view.json'" in grader, \
        "grader does not glob *_view.json — path contract broken"
