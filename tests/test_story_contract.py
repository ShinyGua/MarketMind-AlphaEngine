"""Contract checks for the story/game layer (Phase 3 of the chips overhaul).

Locks in: the story grader's field validation, the anti-templating channel
(anomaly_watch in panel schemas, unconventional_factors verbatim rule in the
moderator), and the chips-analyst role registration.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"

_spec = importlib.util.spec_from_file_location(
    "story_grader", ROOT / "eval" / "graders" / "story_grader.py")
grader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grader)


def _mk_ws(tmp_path, story=None, thesis=None, date="2026-07-16"):
    ws = tmp_path / "WS"
    d = ws / "discussion" / date
    d.mkdir(parents=True)
    if story is not None:
        (d / "story_map.json").write_text(json.dumps(story, ensure_ascii=False))
    if thesis is not None:
        (d / "thesis_map.json").write_text(json.dumps(thesis, ensure_ascii=False))
    return ws


GOOD_STORY = {
    "story": "国药入主后的肿瘤伴随诊断龙头低位再出发，集采落地是唯一的定价开关。",
    "size": {"tier": "medium", "reasoning": "中市值创业板名，故事足以驱动 30-50% 的重估但不是产业级叙事"},
    "certainty": {"tier": "hazy_but_coming", "reasoning": "国药整合方向确定但时点朦胧，集采靴子必然落地"},
    "stage": {"tier": "starting", "reasoning": "接盘价公告已出，机构尚未集中讲这个故事"},
    "teller": ["company", "institutions"],
    "verification_date": "2026-08-31",
    "falsifier": "集采降幅超过 50% 或国药审批被否",
    "market_disagreement": "集采降幅温和还是毁灭性——这是当前价格里唯一真正的分歧",
}


def test_good_story_zero_warnings(tmp_path):
    ws = _mk_ws(tmp_path, story=GOOD_STORY,
                thesis={"unconventional_factors": []})
    result = grader.grade(str(ws), "2026-07-16")
    assert result["pass"] is True
    assert result["warnings"] == []
    assert result["story_present"] is True


def test_missing_story_warns_not_fails(tmp_path):
    ws = _mk_ws(tmp_path, story=None, thesis={"unconventional_factors": []})
    result = grader.grade(str(ws), "2026-07-16")
    assert result["pass"] is True  # advisory
    assert result["story_present"] is False
    assert any("story_map.json missing" in w for w in result["warnings"])


def test_placeholder_story_flagged(tmp_path):
    bad = dict(GOOD_STORY)
    bad["story"] = "<ONE sentence: what story is this stock telling right now>"
    bad["falsifier"] = "TBD"
    ws = _mk_ws(tmp_path, story=bad, thesis={"unconventional_factors": []})
    result = grader.grade(str(ws), "2026-07-16")
    assert any("story field" in w for w in result["warnings"])
    assert any("falsifier" in w for w in result["warnings"])


def test_bad_enum_tier_flagged(tmp_path):
    bad = json.loads(json.dumps(GOOD_STORY))
    bad["certainty"]["tier"] = "definitely_mooning"
    ws = _mk_ws(tmp_path, story=bad, thesis={"unconventional_factors": []})
    result = grader.grade(str(ws), "2026-07-16")
    assert any("certainty.tier" in w for w in result["warnings"])


def test_dropped_unconventional_channel_flagged(tmp_path):
    ws = _mk_ws(tmp_path, story=GOOD_STORY, thesis={"consensus": []})
    result = grader.grade(str(ws), "2026-07-16")
    assert any("unconventional_factors" in w for w in result["warnings"])


# ── prompt-contract checks ───────────────────────────────────────────

def test_panelist_schemas_carry_anomaly_watch():
    for name in ("mm-discussion-panelist", "mm-decision-panelist"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert '"anomaly_watch"' in text, f"{name} ballot/view schema missing anomaly_watch"


def test_decision_ballot_carries_main_force_view():
    text = (SKILLS / "mm-decision-panelist" / "SKILL.md").read_text(encoding="utf-8")
    assert '"main_force_view"' in text
    for stance in ("accumulate", "absorb", "mark_up", "distribute", "avoid"):
        assert stance in text


def test_moderator_verbatim_rule_and_story_map():
    text = (SKILLS / "mm-discussion-moderator" / "SKILL.md").read_text(encoding="utf-8")
    assert "unconventional_factors" in text
    assert "verbatim" in text.lower()
    assert "story_map.json" in text
    for tier in ("hazy_but_coming", "pure_theme", "fermenting"):
        assert tier in text, f"moderator missing story enum {tier}"


def test_decision_maker_chips_rule_and_main_force_read():
    text = (SKILLS / "mm-decision-maker" / "SKILL.md").read_text(encoding="utf-8")
    assert "Chips Are Directional" in text
    assert '"main_force_read"' in text


def test_chips_analyst_registered_and_technical_gone():
    assert (SKILLS / "mm-chips-analyst" / "SKILL.md").exists()
    assert (SKILLS / "mm-chips-analyst" / "agents" / "openai.yaml").exists()
    assert not (SKILLS / "mm-technical-analyst").exists()
    cfg = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "- chips_analyst" in cfg
    assert "- technical_analyst" not in cfg.replace("# - technical_analyst", "")


def test_memo_skills_carry_story_and_offtemplate_sections():
    for name in ("mm-market-analyst", "mm-company-analyst", "mm-risk-analyst",
                 "mm-valuation-analyst", "mm-catalyst-analyst", "mm-chips-analyst"):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        # Language-agnostic: the contract is that the anti-templating section
        # EXISTS, not that it is labelled in Chinese. Asserting the CJK literal
        # forced every memo skill to carry a bilingual heading, which is exactly
        # what leaked into the English MU report.
        assert "框架之外" in text or "Off-Template Factors" in text, \
            f"{name} missing off-template section"
        if name != "mm-chips-analyst":  # the chips memo IS the game lens end-to-end
            assert "故事与博弈" in text or "Story & Game" in text, \
                f"{name} missing story section"


# ── investor-profile (cognition layer) contract ──────────────────────

def test_init_files_carry_investor_questions_in_sync():
    for path in (ROOT / ".claude" / "skills" / "mm-init" / "SKILL.md",
                 ROOT / "plugin" / "commands" / "init.md"):
        text = path.read_text(encoding="utf-8")
        for marker in ("strategy.horizon", "strategy.edge_hypothesis",
                       "strategy.position_state", "investor_profile.json",
                       "胜率最高"):
            assert marker in text, f"{path.name} missing {marker}"


def test_config_has_strategy_block():
    cfg = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "strategy:" in cfg
    assert "edge_hypothesis:" in cfg
    assert "position_state:" in cfg


def test_shared_context_bundles_investor_and_chips():
    text = (ROOT / "scripts" / "build_shared_context.py").read_text(encoding="utf-8")
    for key in ('"chips"', '"peer_divergence"', '"investor"'):
        assert key in text


# ── valuation reference-role (CN/HK) contract ────────────────────────

def test_valuation_role_by_market_defaults():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_valuation", ROOT / "valuation" / "run_valuation.py")
    rv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rv)
    roles = rv._DEFAULTS["role_by_market"]
    assert roles["CN"] == "reference"
    assert roles["HK"] == "reference"
    assert roles["US"] == "anchor"


def test_decision_risk_grader_warns_on_reference_valuation_reason(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "drg", ROOT / "eval" / "graders" / "decision_risk_grader.py")
    drg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drg)
    ws = tmp_path / "600000"
    date = "2026-07-16"
    (ws / "decision" / date).mkdir(parents=True)
    (ws / "valuation" / date).mkdir(parents=True)
    (ws / "decision" / date / "final_decision.json").write_text(json.dumps({
        "decision": "BUY", "confidence": 0.6,
        "top_reasons": ["margin of safety is 50% vs fair value"]}))
    (ws / "valuation" / date / "valuation_summary.json").write_text(json.dumps({
        "applicable": True, "confidence": "high", "role": "reference"}))
    result = drg.grade(str(ws), date)
    assert any("role is 'reference'" in w for w in result["warnings"])
    # anchor role: same citation produces no such warning
    (ws / "valuation" / date / "valuation_summary.json").write_text(json.dumps({
        "applicable": True, "confidence": "high", "role": "anchor"}))
    result2 = drg.grade(str(ws), date)
    assert not any("role is 'reference'" in w for w in result2["warnings"])


def test_render_pdf_reference_label_strings():
    text = (ROOT / "templates" / "render_pdf.py").read_text(encoding="utf-8")
    assert "fair_value_ref" in text
    assert "估值参考（非锚）" in text
    tpl = (ROOT / "templates" / "report.html.j2").read_text(encoding="utf-8")
    assert "fair_value_label" in tpl
