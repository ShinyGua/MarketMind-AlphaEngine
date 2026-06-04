#!/usr/bin/env python3
"""Deduplicate evidence cards across desks/web, then build evidence_digest.json.

The four collectors (company/market/sector desks + mm-web-research) run in
parallel and cannot see each other's cards, so the same article can land as
several cards (e.g. a desk NewsAPI item and a mm-web-research NASDAQ item).
This pass clusters cards by canonical URL and near-identical title, keeps the
single richest card per cluster (highest materiality, then most provenance),
records the merged ids, and writes the deduped digest.

Usage:
    .venv/bin/python3 normalize/dedup_evidence.py {workspace} {date}

Self-degrading: on any error it falls back to a plain concatenation of all
cards so the non-critical normalize stage never blocks the pipeline.
"""
import json
import re
import sys
from glob import glob
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_)", re.I)


def canonical_url(url: str) -> str:
    """Lower-cased host+path with query/fragment and trailing slash stripped."""
    if not url:
        return ""
    try:
        s = urlsplit(url.strip())
        host = (s.netloc or "").lower().lstrip("www.")
        path = (s.path or "").rstrip("/")
        return urlunsplit(("", host, path, "", "")).lstrip("/")
    except Exception:
        return url.strip().lower()


def title_key(title: str) -> str:
    """Normalized title: lower-cased alphanumerics, collapsed whitespace."""
    if not title:
        return ""
    t = re.sub(r"[^a-z0-9一-鿿]+", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def _materiality(card: dict) -> float:
    try:
        return float(card.get("materiality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _richness(card: dict) -> int:
    """Prefer cards carrying more provenance (excerpt, url, published date)."""
    fields = ("source_excerpt", "url", "published_at", "summary",
              "why_it_matters", "provider")
    return sum(1 for f in fields if card.get(f))


def _better(a: dict, b: dict) -> dict:
    """Pick the stronger of two duplicate cards."""
    ka = (_materiality(a), _richness(a), len(json.dumps(a, ensure_ascii=False)))
    kb = (_materiality(b), _richness(b), len(json.dumps(b, ensure_ascii=False)))
    return a if ka >= kb else b


def load_cards(cards_dir: Path) -> list[dict]:
    cards = []
    for f in sorted(glob(str(cards_dir / "*.json"))):
        try:
            cards.append(json.load(open(f, encoding="utf-8")))
        except Exception:
            continue
    return cards


def dedupe(cards: list[dict]) -> list[dict]:
    """Cluster by canonical URL first, then by normalized title."""
    by_key: dict[str, dict] = {}
    for card in cards:
        cu = canonical_url(card.get("url", ""))
        tk = title_key(card.get("title", ""))
        key = f"u:{cu}" if cu else (f"t:{tk}" if tk else f"id:{card.get('id')}")
        if key in by_key:
            kept = _better(by_key[key], card)
            dropped = card if kept is by_key[key] else by_key[key]
            merged = kept.setdefault("merged_ids", [])
            did = dropped.get("id")
            if did and did != kept.get("id") and did not in merged:
                merged.append(did)
            by_key[key] = kept
        else:
            by_key[key] = card
    return list(by_key.values())


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: dedup_evidence.py {workspace} {date}", file=sys.stderr)
        return 2
    workspace, date = Path(sys.argv[1]), sys.argv[2]
    cards_dir = workspace / "normalized" / date / "evidence_cards"
    digest_path = workspace / "normalized" / date / "evidence_digest.json"

    cards = load_cards(cards_dir)
    try:
        deduped = dedupe(cards)
        removed = len(cards) - len(deduped)
    except Exception as e:  # degrade to plain concat
        deduped, removed = cards, 0
        print(f"dedup_evidence: degraded to concat ({type(e).__name__})", file=sys.stderr)

    digest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(digest_path, "w", encoding="utf-8") as out:
        json.dump(deduped, out, ensure_ascii=False, indent=2)
    print(f"evidence_digest.json: {len(deduped)} cards "
          f"({removed} duplicate(s) merged from {len(cards)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
