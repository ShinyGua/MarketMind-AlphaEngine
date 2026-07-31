#!/usr/bin/env python3
"""Story grader: check that the discussion synthesis produced a real story map.

Warning-only (advisory, non-mutating — like decision_risk_grader): a missing or
stub story_map.json degrades the game-layer read but never fails the release.
Checks:
  - story_map.json exists and parses
  - required fields present with valid enum tiers
  - the four core answers are substantive (not empty / placeholder / one word)
  - anti-templating channel intact: thesis_map.unconventional_factors exists
    (may legitimately be an empty list, but the KEY must be there — its absence
    means the moderator dropped the off-template channel entirely)

Usage:
    .venv/bin/python3 eval/graders/story_grader.py <workspace> <date>
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import grader_result_path, story_map_path, thesis_map_path

_SIZE_TIERS = {"large", "medium", "small"}
_CERTAINTY_TIERS = {"confirmed", "high_probability", "hazy_but_coming", "pure_theme"}
_STAGE_TIERS = {"untold", "starting", "fermenting", "consensus", "realized", "falsified"}
_TELLERS = {"company", "institutions", "media", "hot_money"}

_PLACEHOLDER_MARKERS = ("<", "TODO", "TBD", "placeholder", "一句话", "ONE sentence")
_MIN_SUBSTANCE = 15  # chars — a real answer, not a token


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _substantive(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < _MIN_SUBSTANCE:
        return False
    return not any(m in text for m in _PLACEHOLDER_MARKERS)


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    result = {"grader": "story", "pass": True, "warnings": [], "errors": []}

    story = load_json(story_map_path(ws, date))
    if story is None:
        result["warnings"].append("story_map.json missing or unparseable — "
                                  "the game layer has no synthesized story")
        result["story_present"] = False
    else:
        result["story_present"] = True
        if not _substantive(story.get("story")):
            result["warnings"].append("story field empty/placeholder — the four "
                                      "game questions were not really answered")
        for block, tiers in (("size", _SIZE_TIERS), ("certainty", _CERTAINTY_TIERS),
                             ("stage", _STAGE_TIERS)):
            b = story.get(block) or {}
            tier = b.get("tier")
            if tier not in tiers:
                result["warnings"].append(
                    f"{block}.tier {tier!r} not in {sorted(tiers)}")
            if not _substantive(b.get("reasoning")):
                result["warnings"].append(f"{block}.reasoning is empty/placeholder")
        tellers = story.get("teller")
        if not isinstance(tellers, list) or not tellers:
            result["warnings"].append("teller missing — who is telling the story?")
        elif any(t not in _TELLERS for t in tellers):
            result["warnings"].append(f"teller entries outside {sorted(_TELLERS)}")
        if not _substantive(story.get("falsifier")):
            result["warnings"].append("falsifier empty — a story you can't kill "
                                      "is a slogan, not a thesis")

    thesis = load_json(thesis_map_path(ws, date))
    if thesis is not None and "unconventional_factors" not in thesis:
        result["warnings"].append("thesis_map has no unconventional_factors key — "
                                  "the anti-templating channel was dropped in synthesis")

    # advisory: warnings never fail the gate, but surface them
    result["pass"] = True
    result["warning_count"] = len(result["warnings"])
    return result


def main():
    if len(sys.argv) < 3:
        print("usage: story_grader.py <workspace> <date>")
        sys.exit(0)
    ws, date = Path(sys.argv[1]), sys.argv[2]
    result = grade(str(ws), date)
    out_path = grader_result_path(ws, date, "story")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
