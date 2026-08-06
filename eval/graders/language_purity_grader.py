#!/usr/bin/env python3
"""Language-purity grader: one language per report, driven by config.

The pipeline supports `language: en` and `language: ch`, and a run must emit
exactly ONE of them. It previously did not: skills hardcoded bilingual literals
(`## Story & Game (故事与博弈)`) that the model copied verbatim, and three
deterministic scripts wrote `{"en": …, "ch": …}` dicts into artifacts that the
LLM then read whole. An English MU report shipped with 12 Chinese tokens.

Detection is deliberately asymmetric, because "CJK present" and "Chinese prose
present" are different questions:

  Signal A — fullwidth/CJK punctuation on an `en` run. The high-precision one.
      A Chinese COMPANY NAME never carries a full stop, so 。；，、（） appear
      only inside generated Chinese prose. Zero false positives on 金力永磁,
      on a ticker, or on an akshare column name.
  Signal B — ideograph runs on an `en` run, allowlist-filtered. Lower
      precision, so it is suppressed for genuinely-Chinese DATA: anything under
      `cn_flows` (verbatim upstream akshare records that must stay auditable),
      proper nouns harvested from this run's own profile/peer_set, and evidence
      cards quoting a foreign-language source.
  Signal C — Latin prose on a `ch` run. "Any Latin = fail" is useless here:
      English JSON keys, enum values and BUY/HOLD/SELL are mandated by
      contract. So this is sentence-level, and in JSON only under prose keys.

Hard-fails only on surfaces that are 100% our own templates — the final report,
the shared_context bundle the analysts read, and the two deterministic `note`
fields. Everything else warns. Legacy pre-fix artifact shapes warn rather than
error so the grader is landable while old workspaces still hold them.

Usage:
    .venv/bin/python3 eval/graders/language_purity_grader.py <workspace> <date>
"""
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import (chip_structure_path, detect_final_report,  # noqa: E402
                              grader_result_path, intraday_timing_path,
                              resolve_language, shared_context_read_path)

DEFAULTS = {
    "min_cjk_run": 2,       # ideographs; kills single-char noise
    "min_latin_words": 6,   # a sentence, not a metric name
    "excerpt_chars": 90,
}

# Signal A. Fullwidth punctuation + CJK brackets/quotes. Never appears in a
# proper noun — this is the marker of generated Chinese PROSE.
_FULLWIDTH = re.compile(r"[　-〿！-･]")
_IDEOGRAPH_RUN = re.compile(r"[一-鿿㐀-䶿]+")

# Values under these JSON keys are prose and follow the report language.
# Everything else in these artifacts is a key, an enum or an id — English by
# contract in BOTH languages (AGENTS.md).
# Deliberately excludes `usage_note`, `usage`, `reason` and similar: those are
# the pipeline's own machine-facing contract strings ("First-class directional
# evidence…", "no_daily_data") and are English in BOTH languages by design.
_PROSE_KEYS = {"note", "summary", "title", "why_it_matters", "reasoning",
               "story", "falsifier", "market_disagreement", "rationale"}

# Subtrees excluded from the language check:
#   cn_flows  — verbatim upstream akshare records; genuinely Chinese, must stay
#               auditable, and has no English equivalent
#   metadata  — collector provenance ("sourced from yfinance calendar, not an
#               HKEX filing"), English by convention in both languages
_DATA_SUBTREES = ("cn_flows", "metadata")

_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_TICKER = re.compile(r"\b[A-Z0-9][A-Z0-9.\-]{0,7}\b")
_CODE = re.compile(r"`[^`]*`|```.*?```", re.S)
_URL = re.compile(r"https?://\S+")
_EV_ID = re.compile(r"\bev_[\w\-]+")
# Image/link chrome: `![Price action & technicals](charts/price_chart.svg)`.
# The alt text is renderer furniture, not report prose, and is English in both
# languages because it names a file the template embeds.
_MD_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_thresholds(ws: Path) -> dict:
    th = dict(DEFAULTS)
    cfg = load_json(ws / "resolved_config.json") or {}
    user = (cfg.get("review") or {}).get("language_purity") or {}
    for k in DEFAULTS:
        if isinstance(user.get(k), int):
            th[k] = user[k]
    return th


def proper_nouns(ws: Path) -> set[str]:
    """Chinese strings that are legitimately present regardless of language.

    Harvested from the run's OWN data rather than hardcoded, so a Chinese
    company name never trips the ideograph signal.
    """
    out: set[str] = set()

    def harvest(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and _IDEOGRAPH_RUN.search(v) and len(v) <= 40:
                    if "name" in k or k in ("short_name", "company_name", "alias"):
                        out.add(v.strip())
                else:
                    harvest(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest(v)

    for p in (ws / "profile" / "company_profile.json", ws / "profile" / "peer_set.json"):
        harvest(load_json(p))
    return {s for s in out if s}


def _strip_known(text: str, nouns: set[str]) -> str:
    for n in sorted(nouns, key=len, reverse=True):
        text = text.replace(n, " ")
    return text


def scan_text(text: str, lang: str, nouns: set[str], th: dict) -> list[dict]:
    """Signals A/B (en) or C (ch) over a markdown/plain body."""
    hits: list[dict] = []
    if lang == "en":
        for m in _FULLWIDTH.finditer(text):
            hits.append({"signal": "fullwidth_punctuation",
                         "excerpt": text[max(0, m.start() - 40):m.start() + 20].strip()})
            break  # one is enough to fail; excerpt shows where
        cleaned = _strip_known(text, nouns)
        for m in _IDEOGRAPH_RUN.finditer(cleaned):
            if len(m.group()) >= th["min_cjk_run"]:
                hits.append({"signal": "chinese_ideographs",
                             "excerpt": cleaned[max(0, m.start() - 40):m.end() + 20].strip()})
                break
    else:
        body = _MD_LINK.sub(" ", _URL.sub(" ", _CODE.sub(" ", text)))
        body = _EV_ID.sub(" ", body)
        for raw in re.split(r"[。！？.!?\n]", body):
            s = raw.strip()
            if not s or s.startswith("|") or _IDEOGRAPH_RUN.search(s):
                continue
            words = [w for w in _LATIN_WORD.findall(_TICKER.sub(" ", s))
                     if not w.isupper()]
            if len(words) >= th["min_latin_words"]:
                hits.append({"signal": "english_prose_in_ch_run", "excerpt": s[:th["excerpt_chars"]]})
                break
    return hits


def scan_json(obj, lang: str, nouns: set[str], th: dict, path: str = "") -> list[dict]:
    hits: list[dict] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _DATA_SUBTREES:
                continue  # verbatim upstream source data — auditable, not prose
            hits += scan_json(v, lang, nouns, th, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += scan_json(v, lang, nouns, th, f"{path}[{i}]")
    elif isinstance(obj, str):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if lang == "en":
            for h in scan_text(obj, lang, nouns, th):
                hits.append({**h, "path": path})
        elif leaf in _PROSE_KEYS:
            for h in scan_text(obj, lang, nouns, th):
                hits.append({**h, "path": path})
    return hits


def _legacy_shape(doc, field: str) -> bool:
    """Pre-fix artifact: a bilingual dict where a single string is now written."""
    return isinstance(doc, dict) and isinstance(doc.get(field), dict) \
        and {"en", "ch"} <= set(doc[field])


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    th = _load_thresholds(ws)
    lang = resolve_language(ws)
    nouns = proper_nouns(ws)
    result = {"grader": "language_purity", "workspace": str(ws), "date": date,
              "language": lang, "violations": [], "warnings": [], "errors": [],
              "checked_files": [], "details": []}

    def record(hits, label, hard):
        for h in hits:
            entry = {"file": label, **h}
            result["violations"].append(entry)
            msg = f"{label}: {h['signal']} — {h.get('excerpt', '')[:th['excerpt_chars']]}"
            (result["errors"] if hard else result["warnings"]).append(msg)

    # 1. the final report — the shipped surface
    report = detect_final_report(ws, date)
    if report and report.exists():
        result["checked_files"].append(str(report.name))
        record(scan_text(report.read_text(encoding="utf-8"), lang, nouns, th),
               report.name, hard=True)

    # 2. shared_context — what the analysts actually read
    sc = shared_context_read_path(ws, date)
    doc = load_json(sc)
    if doc is not None:
        result["checked_files"].append(sc.name)
        record(scan_json(doc, lang, nouns, th), sc.name, hard=True)

    # 3/4. the two deterministic note fields — 100% our own templates
    for path_fn, field in ((chip_structure_path, "note"), (intraday_timing_path, "note")):
        p = path_fn(ws, date)
        d = load_json(p)
        if d is None:
            continue
        result["checked_files"].append(p.name)
        if _legacy_shape(d, field):
            result["warnings"].append(
                f"{p.name}: legacy bilingual `{field}` dict — predates the single-language fix; "
                f"re-run the script to regenerate")
        elif isinstance(d.get(field), str):
            record([{**h, "path": field} for h in scan_text(d[field], lang, nouns, th)],
                   p.name, hard=True)

    # 5. evidence cards — advisory (titles may legitimately quote a source)
    cards_dir = ws / "normalized" / date / "evidence_cards"
    for cp in sorted(cards_dir.glob("*.json")) if cards_dir.is_dir() else []:
        c = load_json(cp)
        if isinstance(c, dict) and c.get("url"):
            continue  # quoting a foreign-language source is legitimate
        record(scan_json(c, lang, nouns, th), cp.name, hard=False)

    result["pass"] = not result["errors"]
    result["warning_count"] = len(result["warnings"])
    result["details"].append(
        f"language={lang}; {len(result['checked_files'])} file(s) checked; "
        f"{len(result['errors'])} error(s), {len(result['warnings'])} warning(s)")
    return result


def main():
    if len(sys.argv) < 3:
        print("usage: language_purity_grader.py <workspace> <date>")
        sys.exit(0)
    ws, date = Path(sys.argv[1]), sys.argv[2]
    result = grade(str(ws), date)
    out_path = grader_result_path(ws, date, "language_purity")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
