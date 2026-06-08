"""Guard the de-weighting of DCF/valuation in the discussion + decision skills.

Valuation (DCF and the blended canonical fair value) is one evidence-weighted
*reference*, not a standalone BUY/SELL trigger. This locks in the wording so a
future edit can't silently re-anchor the decision on valuation.

Phrase-specific by design: it bans only the exact over-weighting phrases and
asserts the exact replacement phrases. Legitimate valuation vocabulary
("margin of safety", "cheap", "expensive", "fair value") stays legal and is NOT
policed here.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"

DECISION_MAKER = SKILLS / "mm-decision-maker" / "SKILL.md"
PANELIST = SKILLS / "mm-decision-panelist" / "SKILL.md"
VALUATION = SKILLS / "mm-valuation-analyst" / "SKILL.md"

# Exact over-weighting phrases that must NOT reappear in any of the three skills.
BANNED = [
    "Valuation-Anchored",
    "valuation anchor",
    "anchors on margin of safety",
    "negative margin of safety is a genuine sell signal",
]


def _text(p):
    return p.read_text(encoding="utf-8")


def test_no_banned_overweighting_phrases():
    offenders = []
    for skill in (DECISION_MAKER, PANELIST, VALUATION):
        body = _text(skill)
        for phrase in BANNED:
            if phrase in body:
                offenders.append(f"{skill.name}: {phrase!r}")
    assert not offenders, f"over-weighting phrases re-introduced: {offenders}"


def test_decision_maker_is_valuation_aware():
    assert "Valuation-Aware" in _text(DECISION_MAKER)


def test_panelist_treats_valuation_as_reference():
    assert "valuation reference" in _text(PANELIST)


def test_valuation_analyst_carries_reduced_decision_weight():
    assert "reduced decision weight" in _text(VALUATION)
