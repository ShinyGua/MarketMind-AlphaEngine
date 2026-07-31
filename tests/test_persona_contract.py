"""Contract checks for the persona layer (risk mandates + devil's advocate).

Locks in the design line from the persona review: analytical risk mandates are
allowed, rhetorical personality is not. Phrase-specific by design — it asserts
the exact fence wording and bans only exact rhetorical-persona phrases, so
legitimate vocabulary stays legal.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"

MEMO_SKILLS = ("mm-market-analyst", "mm-company-analyst", "mm-risk-analyst",
               "mm-valuation-analyst", "mm-chips-analyst", "mm-catalyst-analyst")
PANELIST_SKILLS = ("mm-discussion-panelist", "mm-decision-panelist")

# The fence that keeps mandates analytical: must survive verbatim in every copy.
FENCE = "The mandate shapes *what you weigh*, not *how you speak*"

# Rhetorical-persona phrases that must never appear in any panel/memo skill.
BANNED_PERSONA = ("act confident", "aggressive personality", "speak forcefully",
                  "persuasive tone", "你是一位自信")


def _text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_all_persona_skills_carry_the_mandate_and_fence():
    for name in MEMO_SKILLS + PANELIST_SKILLS:
        text = _text(name)
        assert "analyst_risk_profiles" in text, f"{name} missing the risk-profile lookup"
        assert FENCE in text, f"{name} missing the analytical fence"
        assert "risk_averse" in text and "risk_neutral" in text, \
            f"{name} missing the two mandate profiles"


def test_no_rhetorical_persona_phrases():
    for name in MEMO_SKILLS + PANELIST_SKILLS:
        text = _text(name)
        for phrase in BANNED_PERSONA:
            assert phrase not in text, f"{name} contains rhetorical persona: {phrase!r}"


def test_panelists_handle_devils_advocate_without_breaking_isolation():
    for name in PANELIST_SKILLS:
        text = _text(name)
        assert "devils_advocate" in text, f"{name} missing the devil's-advocate step"
        assert "convergence_round_{N-1}.json" in text, \
            f"{name} cannot see the grader output that names the devil's advocate"
        # the DA keeps their honest position — never manufactures dissent
        assert "do NOT flip to manufacture dissent" in text, \
            f"{name} missing the honest-stance rule for the devil's advocate"
        # prior-round isolation from other roles' files must survive
        assert "do NOT read other" in text, f"{name} lost the prior-round isolation rule"


def test_graders_name_devils_advocate_field():
    for grader in ("discussion_convergence_grader.py", "panel_convergence_grader.py"):
        text = (ROOT / "eval" / "graders" / grader).read_text(encoding="utf-8")
        assert '"devils_advocate"' in text, f"{grader} missing the devils_advocate field"
        assert "unanimity_challenge" in text, f"{grader} missing the challenge exit reason"
