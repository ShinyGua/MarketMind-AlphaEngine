#!/usr/bin/env python3
"""Evidence grader: check that high-materiality evidence cards are cited in the report."""

import sys
import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp"))
from shared.contracts import detect_final_report, grader_result_path


def load_json(path: Path):
    """Load JSON file, return None if missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def extract_keywords(title: str) -> list[str]:
    """Extract meaningful words from a title (length >= 3, lowercased)."""
    # Remove punctuation and split
    words = re.findall(r"[a-zA-Z0-9]+", title.lower())
    # Filter out short words and common stop words
    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "has", "have", "its", "but", "not", "new", "all", "can", "may",
        "will", "also", "than", "been", "into", "over", "more", "about",
    }
    kws = [w for w in words if len(w) >= 3 and w not in stop_words]
    # Chinese titles have no spaces and match no [a-zA-Z0-9]+ words, which made
    # the >=3-keyword fallback unreachable (cards then pass only by exact id).
    # Slice each CJK run into overlapping bigrams so paraphrased citations count.
    for run in re.findall(r"[一-鿿]{2,}", title):
        kws.extend(run[i:i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(kws))  # dedupe, keep order


def check_keyword_overlap(title_keywords: list[str], report_text_lower: str, min_overlap: int = 3) -> bool:
    """Check if at least min_overlap keywords from the title appear in the report."""
    found = sum(1 for kw in title_keywords if kw in report_text_lower)
    return found >= min_overlap


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)
    digest_path = ws / "normalized" / date / "evidence_digest.json"
    report_path = detect_final_report(ws, date)

    result = {
        "grader": "evidence_coverage",
        "pass": False,
        "high_materiality_cards": 0,
        "cited_cards": 0,
        "missed_cards": [],
        "coverage_ratio": 0.0,
        "errors": [],
    }

    digest = load_json(digest_path)
    if digest is None:
        result["errors"].append(f"Evidence digest not found or invalid: {digest_path}")

    report_text = None
    if report_path is None:
        result["errors"].append(f"Report not found under: {ws / 'final' / date}")
    else:
        try:
            report_text = report_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            result["errors"].append(f"Report not found: {report_path}")

    if digest is None or report_text is None:
        return result

    # Filter high-materiality cards
    high_mat_cards = [c for c in digest if c.get("materiality_score", 0) >= 0.7]
    result["high_materiality_cards"] = len(high_mat_cards)

    if len(high_mat_cards) == 0:
        result["pass"] = True
        result["coverage_ratio"] = 1.0
        return result

    report_lower = report_text.lower()
    cited = 0

    for card in high_mat_cards:
        card_id = card.get("id", "")
        title = card.get("title", "")
        materiality = card.get("materiality_score", 0)

        # Check 1: exact ID match
        id_found = card_id in report_text

        # Check 2: fuzzy keyword match from title
        keywords = extract_keywords(title)
        keyword_found = check_keyword_overlap(keywords, report_lower, min_overlap=3)

        covered = id_found or keyword_found

        if covered:
            cited += 1
        else:
            result["missed_cards"].append({
                "id": card_id,
                "title": title,
                "materiality": materiality,
            })

    result["cited_cards"] = cited
    result["coverage_ratio"] = round(cited / len(high_mat_cards), 4) if high_mat_cards else 1.0
    result["pass"] = result["coverage_ratio"] >= 0.8

    return result


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <workspace> <date>", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    date = sys.argv[2]

    result = grade(workspace, date)

    # Write output
    ws = Path(workspace)
    out_path = grader_result_path(ws, date, "evidence")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
