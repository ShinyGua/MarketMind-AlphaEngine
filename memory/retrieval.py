#!/usr/bin/env python3
"""Memory retrieval for MarketMind pipeline stages.

Retrieves relevant memories scored by importance, confidence, and recency
for use in analyst discussion, report writing, and review stages.

Usage:
    .venv/bin/python3 memory/retrieval.py {workspace} {date} {query_type}

Where query_type is one of:
    analyst  -- episodic + semantic for ticker/sector (before Stage 6)
    writer   -- procedural + recent episodic for ticker (before Stage 9)
    reviewer -- procedural only (before Stage 10)
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEMORY_ROOT = Path(__file__).resolve().parent  # memory/

INDEX_PATHS = {
    "episodic":   MEMORY_ROOT / "episodic"   / "index.jsonl",
    "semantic":   MEMORY_ROOT / "semantic"    / "index.jsonl",
    "procedural": MEMORY_ROOT / "procedural" / "index.jsonl",
}

HALF_LIVES = {
    "episodic":   30,
    "semantic":   180,
    "procedural": 99999,
}

DEFAULT_TOP_K = 10

# Which memory types each query_type reads
QUERY_TYPES = {
    "analyst":  ["episodic", "semantic"],
    "writer":   ["procedural", "episodic"],
    "reviewer": ["procedural"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return a list of parsed objects.

    Returns an empty list if the file does not exist or is empty.
    Skips malformed lines silently.
    """
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def load_company_profile(workspace: Path) -> tuple[str | None, str | None]:
    """Return (ticker, sector) from the workspace profile, or (None, None)."""
    profile_path = workspace / "profile" / "company_profile.json"
    if not profile_path.exists():
        return None, None
    try:
        with open(profile_path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
        ticker = profile.get("ticker")
        sector = profile.get("sector") or profile.get("sector_profile")
        return ticker, sector
    except (json.JSONDecodeError, OSError):
        return None, None


def load_memory_config(workspace: Path) -> dict:
    """Load memory-related config values from resolved_config.json.

    Falls back to sensible defaults if the file is missing or lacks a
    'memory' section.
    """
    defaults = {"top_k": DEFAULT_TOP_K}
    config_path = workspace / "resolved_config.json"
    if not config_path.exists():
        return defaults
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        mem_cfg = cfg.get("memory", {})
        return {
            "top_k": mem_cfg.get("top_k", defaults["top_k"]),
        }
    except (json.JSONDecodeError, OSError):
        return defaults


def score_memory(memory: dict, today: datetime, mem_type: str) -> float:
    """Compute a retrieval score combining importance, confidence, and recency.

    score = importance * confidence * exp(-days_old / half_life)
    """
    tags = memory.get("tags", {})
    importance = float(tags.get("importance", 0.5))
    confidence = float(tags.get("confidence", 0.5))

    created_str = memory.get("created_at", "")
    if created_str:
        try:
            created_dt = datetime.fromisoformat(created_str)
            # Ensure both datetimes are timezone-aware for subtraction
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            days_old = max((today - created_dt).days, 0)
        except (ValueError, TypeError):
            days_old = 0
    else:
        days_old = 0

    half_life = HALF_LIVES.get(mem_type, 30)
    recency = math.exp(-days_old / half_life)

    return importance * confidence * recency


def matches_ticker_or_sector(
    memory: dict,
    ticker: str | None,
    sector: str | None,
) -> bool:
    """Check whether a memory's tags reference the given ticker or sector."""
    tags = memory.get("tags", {})
    mem_ticker = tags.get("ticker", "")
    mem_sector = tags.get("sector", "")

    if ticker and mem_ticker and mem_ticker.upper() == ticker.upper():
        return True
    if sector and mem_sector and mem_sector.lower() == sector.lower():
        return True
    return False


def matches_ticker(memory: dict, ticker: str | None) -> bool:
    """Check whether a memory's tags reference the given ticker."""
    if not ticker:
        return True  # no filtering when ticker unknown
    mem_ticker = memory.get("tags", {}).get("ticker", "")
    return bool(mem_ticker and mem_ticker.upper() == ticker.upper())


# ---------------------------------------------------------------------------
# Core retrieval
# ---------------------------------------------------------------------------

def retrieve(
    workspace: Path,
    run_date: str,
    query_type: str,
) -> dict:
    """Retrieve and score memories for the given query type.

    Returns a dict ready for JSON serialisation with the top-k results.
    """
    if query_type not in QUERY_TYPES:
        raise ValueError(
            f"Unknown query_type '{query_type}'. "
            f"Must be one of: {', '.join(QUERY_TYPES)}"
        )

    ticker, sector = load_company_profile(workspace)
    mem_config = load_memory_config(workspace)
    top_k = mem_config.get("top_k", DEFAULT_TOP_K)

    today = datetime.now(timezone.utc)

    candidates: list[dict] = []

    for mem_type in QUERY_TYPES[query_type]:
        entries = load_jsonl(INDEX_PATHS[mem_type])

        for entry in entries:
            # Skip superseded memories
            if entry.get("superseded_by"):
                continue

            # Apply type-specific filtering
            if query_type == "analyst":
                # episodic + semantic: must match ticker OR sector
                if ticker or sector:
                    if not matches_ticker_or_sector(entry, ticker, sector):
                        continue

            elif query_type == "writer":
                if mem_type == "episodic":
                    # episodic: must match ticker
                    if not matches_ticker(entry, ticker):
                        continue
                # procedural: no ticker filter — include all

            # reviewer: procedural only, no filtering needed

            score = score_memory(entry, today, mem_type)
            candidates.append({
                "id": entry.get("id", ""),
                "type": mem_type,
                "content": entry.get("content", ""),
                "tags": entry.get("tags", {}),
                "score": round(score, 6),
                "created_at": entry.get("created_at", ""),
            })

    # Sort by score descending
    candidates.sort(key=lambda m: m["score"], reverse=True)

    # For writer query, limit episodic to 3 most recent (by created_at)
    if query_type == "writer":
        procedural = [m for m in candidates if m["type"] == "procedural"]
        episodic = [m for m in candidates if m["type"] == "episodic"]
        # Keep only 3 most recent episodic (already sorted by score;
        # re-sort this subset by created_at descending to pick recency)
        episodic.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        episodic = episodic[:3]
        # Merge back and re-sort by score
        candidates = procedural + episodic
        candidates.sort(key=lambda m: m["score"], reverse=True)

    # Apply top_k
    candidates = candidates[:top_k]

    result = {
        "query_type": query_type,
        "ticker": ticker,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "memories": candidates,
        "count": len(candidates),
    }
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 4:
        print(
            "Usage: .venv/bin/python3 memory/retrieval.py "
            "{workspace} {date} {query_type}",
            file=sys.stderr,
        )
        print(
            "  query_type: analyst | writer | reviewer",
            file=sys.stderr,
        )
        sys.exit(1)

    workspace = Path(sys.argv[1]).resolve()
    run_date = sys.argv[2]  # YYYY-MM-DD
    query_type = sys.argv[3]

    if not workspace.is_dir():
        print(f"Error: workspace '{workspace}' is not a directory", file=sys.stderr)
        sys.exit(1)

    try:
        result = retrieve(workspace, run_date, query_type)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Write per-role output file to workspace (avoids cross-role overwrite)
    output_path = workspace / f"{run_date}_memory_context_{query_type}.json"
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    # Also print to stdout
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
