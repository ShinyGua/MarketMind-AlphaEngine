"""
MCP Memory Server for MarketMind-AlphaEngine.

Long-term memory storage and retrieval across equity research runs.
Three types: episodic (per-run snapshots), semantic (durable facts),
procedural (quality-improvement learnings).
Storage: append-only JSONL files under memory/{type}/index.jsonl
"""
from __future__ import annotations

import json, math, os, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types

PROJECT_ROOT = Path(__file__).parent.parent
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(PROJECT_ROOT / "memory")))
MEMORY_TYPES = ("episodic", "semantic", "procedural")
HALF_LIVES = {"episodic": 30, "semantic": 180, "procedural": 99999}

# -- Helpers -----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _ensure_jsonl(mem_type: str) -> Path:
    d = MEMORY_DIR / mem_type
    d.mkdir(parents=True, exist_ok=True)
    fp = d / "index.jsonl"
    if not fp.exists():
        fp.touch()
    return fp

def _read_jsonl(fp: Path) -> list[dict]:
    if not fp.exists():
        return []
    entries: list[dict] = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries

def _write_jsonl(fp: Path, entries: list[dict]) -> None:
    fp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ("\n" if entries else ""), encoding="utf-8")

def _append_jsonl(fp: Path, entry: dict) -> None:
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _days_since(iso_date: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 0)
    except Exception:
        return 0.0

def _generate_id(mem_type: str, tags: dict, source: dict, existing: list[dict]) -> str:
    ticker = (tags.get("ticker") or "global").upper()
    run_date = source.get("run_date") or _today_str()
    prefix = f"mem_{ticker}_{run_date}_{mem_type}_"
    seq = sum(1 for e in existing if e.get("id", "").startswith(prefix)) + 1
    return f"{prefix}{seq}"

def _score_memory(entry: dict, query: str, filters: dict) -> float | None:
    """Score entry against query+filters. Returns None if filtered out."""
    tags = entry.get("tags", {})
    if filters.get("type") and entry.get("type") != filters["type"]:
        return None
    if filters.get("ticker") and tags.get("ticker", "").upper() != filters["ticker"].upper():
        return None
    if filters.get("sector") and tags.get("sector", "").lower() != filters["sector"].lower():
        return None
    if filters.get("event_type") and tags.get("event_type") != filters["event_type"]:
        return None
    if filters.get("min_importance") is not None and (tags.get("importance") or 0) < filters["min_importance"]:
        return None
    if filters.get("since") and entry.get("created_at", "") < filters["since"]:
        return None

    importance = tags.get("importance", 0.5)
    has_kw = query and query.lower() in entry.get("content", "").lower()
    base = importance * (1.0 if has_kw else 0.3)
    hl = HALF_LIVES.get(entry.get("type", "episodic"), 30)
    return base * math.exp(-_days_since(entry.get("created_at", _now_iso())) / hl)

def _slim(entry: dict, score: float | None = None) -> dict:
    out: dict[str, Any] = {k: entry.get(k) for k in ("id", "type", "content", "tags", "created_at")}
    if score is not None:
        out["score"] = round(score, 4)
    return out

def _all_entries(mem_type: str | None = None) -> list[dict]:
    types = [mem_type] if mem_type and mem_type in MEMORY_TYPES else list(MEMORY_TYPES)
    out: list[dict] = []
    for t in types:
        out.extend(_read_jsonl(_ensure_jsonl(t)))
    return out

def _text(data: Any) -> list[mcp.types.TextContent]:
    return [mcp.types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]

def _type_from_id(mid: str) -> str | None:
    for t in MEMORY_TYPES:
        if f"_{t}_" in mid:
            return t
    return None

def _entity_match(entry: dict, entity: str) -> bool:
    ticker = (entry.get("tags", {}).get("ticker") or "").upper()
    related = [r.upper() for r in (entry.get("related_entities") or [])]
    return entity == ticker or entity in related

# -- Server ------------------------------------------------------------------

server = Server("memory")

@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]:
    return [
        mcp.types.Tool(name="store_memory",
            description="Store a new memory unit (episodic, semantic, or procedural).",
            inputSchema={"type": "object", "required": ["type", "content"], "properties": {
                "type": {"type": "string", "enum": ["episodic", "semantic", "procedural"]},
                "content": {"type": "string"},
                "tags": {"type": "object", "properties": {
                    "ticker": {"type": "string"}, "sector": {"type": "string"},
                    "event_type": {"type": "string"}, "decision": {"type": "string"},
                    "confidence": {"type": "number"}, "horizon": {"type": "string"},
                    "key_themes": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "number"}}},
                "related_entities": {"type": "array", "items": {"type": "string"}},
                "source_run": {"type": "object", "properties": {
                    "run_date": {"type": "string"}, "workspace": {"type": "string"},
                    "stage": {"type": "string"}}}}}),
        mcp.types.Tool(name="search_memory",
            description="Search memories by keyword query and optional filters.",
            inputSchema={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"},
                "filters": {"type": "object", "properties": {
                    "type": {"type": "string"}, "ticker": {"type": "string"},
                    "sector": {"type": "string"}, "event_type": {"type": "string"},
                    "min_importance": {"type": "number"}, "since": {"type": "string"}}},
                "limit": {"type": "integer", "default": 10}}}),
        mcp.types.Tool(name="get_entity_timeline",
            description="Get chronological timeline of all memories for a given entity.",
            inputSchema={"type": "object", "required": ["entity"], "properties": {
                "entity": {"type": "string"},
                "limit": {"type": "integer", "default": 20}}}),
        mcp.types.Tool(name="update_memory",
            description="Update tags or mark a memory as superseded.",
            inputSchema={"type": "object", "required": ["memory_id"], "properties": {
                "memory_id": {"type": "string"},
                "tags": {"type": "object"},
                "superseded_by": {"type": "string"}}}),
        mcp.types.Tool(name="prune_memories",
            description="Remove old, low-importance, superseded memories. Procedural never pruned.",
            inputSchema={"type": "object", "properties": {
                "older_than_days": {"type": "integer", "default": 180},
                "min_importance": {"type": "number", "default": 0.3},
                "memory_type": {"type": "string"}}}),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]:
    try:
        handler = {"store_memory": _store, "search_memory": _search,
                    "get_entity_timeline": _timeline, "update_memory": _update,
                    "prune_memories": _prune}.get(name)
        if not handler:
            return _text({"error": f"Unknown tool: {name}"})
        return handler(arguments)
    except Exception as exc:
        return _text({"error": str(exc)})

def _store(args: dict) -> list[mcp.types.TextContent]:
    mem_type = args["type"]
    if mem_type not in MEMORY_TYPES:
        return _text({"error": f"Invalid memory type: {mem_type}"})
    tags = args.get("tags") or {}
    source = args.get("source_run") or {}
    fp = _ensure_jsonl(mem_type)
    existing = _read_jsonl(fp)
    mem_id = _generate_id(mem_type, tags, source, existing)
    now = _now_iso()
    entry = {"id": mem_id, "type": mem_type, "content": args["content"], "tags": tags,
             "related_entities": args.get("related_entities") or [], "source": source,
             "created_at": now, "updated_at": now, "superseded_by": None}
    _append_jsonl(fp, entry)
    return _text({"stored": True, "id": mem_id, "type": mem_type})

def _search(args: dict) -> list[mcp.types.TextContent]:
    query = args.get("query", "")
    filters = args.get("filters") or {}
    limit = args.get("limit", 10)
    entries = _all_entries(filters.get("type"))
    scored = [(s, e) for e in entries if (s := _score_memory(e, query, filters)) is not None]
    scored.sort(key=lambda x: x[0], reverse=True)
    return _text({"results": [_slim(e, s) for s, e in scored[:limit]], "total_scanned": len(entries)})

def _timeline(args: dict) -> list[mcp.types.TextContent]:
    entity = args["entity"].upper()
    limit = args.get("limit", 20)
    matched = sorted([e for e in _all_entries() if _entity_match(e, entity)],
                     key=lambda e: e.get("created_at", ""))[:limit]
    return _text({"entity": entity, "timeline": [_slim(e) for e in matched], "count": len(matched)})

def _update(args: dict) -> list[mcp.types.TextContent]:
    mid = args["memory_id"]
    target = _type_from_id(mid)
    for mem_type in ([target] if target else list(MEMORY_TYPES)):
        fp = _ensure_jsonl(mem_type)
        entries = _read_jsonl(fp)
        for entry in entries:
            if entry.get("id") == mid:
                if args.get("tags"):
                    entry.setdefault("tags", {}).update(args["tags"])
                if args.get("superseded_by") is not None:
                    entry["superseded_by"] = args["superseded_by"]
                entry["updated_at"] = _now_iso()
                _write_jsonl(fp, entries)
                return _text({"updated": True, "id": mid})
    return _text({"error": f"Memory not found: {mid}"})

def _prune(args: dict) -> list[mcp.types.TextContent]:
    max_age = args.get("older_than_days", 180)
    min_imp = args.get("min_importance", 0.3)
    target = args.get("memory_type")
    types = [t for t in ([target] if target and target in MEMORY_TYPES else list(MEMORY_TYPES))
             if t != "procedural"]
    pruned = 0
    remaining = 0
    for mt in types:
        fp = _ensure_jsonl(mt)
        entries = _read_jsonl(fp)
        keep = []
        for e in entries:
            age = _days_since(e.get("created_at", _now_iso()))
            imp = e.get("tags", {}).get("importance", 0.5)
            if age > max_age and imp < min_imp and e.get("superseded_by") is not None:
                pruned += 1
            else:
                keep.append(e)
        _write_jsonl(fp, keep)
        remaining += len(keep)
    remaining += len(_read_jsonl(_ensure_jsonl("procedural")))
    return _text({"pruned": pruned, "remaining": remaining})

# -- Resources ---------------------------------------------------------------

@server.list_resources()
async def list_resources() -> list[mcp.types.Resource]:
    return [
        mcp.types.Resource(uri="memory://index", name="Memory Index",
            description="Summary counts and latest dates for all memory types.",
            mimeType="application/json"),
        mcp.types.Resource(uri="memory://recent", name="Recent Memories",
            description="The 20 most recent memories across all types.",
            mimeType="application/json"),
    ]

@server.read_resource()
async def read_resource(uri: str) -> str:
    try:
        uri_str = str(uri)
        if uri_str == "memory://index":
            summary = {}
            for mt in MEMORY_TYPES:
                entries = _read_jsonl(_ensure_jsonl(mt))
                dates = [e.get("created_at", "") for e in entries]
                summary[mt] = {"count": len(entries), "latest": max(dates) if dates else None}
            return json.dumps(summary, indent=2)
        if uri_str == "memory://recent":
            entries = sorted(_all_entries(), key=lambda e: e.get("created_at", ""), reverse=True)
            return json.dumps({"recent": [_slim(e) for e in entries[:20]], "count": min(len(entries), 20)}, indent=2)
        m = re.match(r"memory://entity/(.+)", uri_str)
        if m:
            entity = m.group(1).upper()
            matched = sorted([e for e in _all_entries() if _entity_match(e, entity)],
                             key=lambda e: e.get("created_at", ""))
            return json.dumps({"entity": entity, "timeline": [_slim(e) for e in matched],
                               "count": len(matched)}, indent=2)
        return json.dumps({"error": f"Unknown resource URI: {uri_str}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

# -- Prompts -----------------------------------------------------------------

@server.list_prompts()
async def list_prompts() -> list[mcp.types.Prompt]:
    return [
        mcp.types.Prompt(name="analysis_reflection",
            description="Reflect on a completed analysis run and create memory entries.",
            arguments=[
                mcp.types.PromptArgument(name="ticker", description="Stock ticker", required=True),
                mcp.types.PromptArgument(name="date", description="Run date YYYY-MM-DD", required=True)]),
        mcp.types.Prompt(name="lesson_extraction",
            description="Extract procedural learnings from review failures.",
            arguments=[
                mcp.types.PromptArgument(name="ticker", description="Stock ticker", required=True),
                mcp.types.PromptArgument(name="date", description="Run date YYYY-MM-DD", required=True)]),
    ]

@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> mcp.types.GetPromptResult:
    a = arguments or {}
    ticker = a.get("ticker", "UNKNOWN")
    date = a.get("date", _today_str())
    prompts = {
        "analysis_reflection": (
            f"Reflect on {ticker} analysis run from {date}",
            f"Review the analysis run for {ticker} on {date}. Read the final decision, review "
            f"scores, and thesis map. Create an episodic memory summarizing the key decision, "
            f"confidence, and themes. Extract any semantic learnings about the company or sector. "
            f"If the review required revisions, extract procedural learnings about common errors."),
        "lesson_extraction": (
            f"Extract lessons from {ticker} review on {date}",
            f"Read the score_history.json and revision_briefs for {ticker} on {date}. For each "
            f"blocker or failure found, create a procedural memory describing the error pattern "
            f"and how to avoid it in future analyses."),
    }
    if name not in prompts:
        raise ValueError(f"Unknown prompt: {name}")
    desc, text = prompts[name]
    return mcp.types.GetPromptResult(description=desc, messages=[
        mcp.types.PromptMessage(role="user",
            content=mcp.types.TextContent(type="text", text=text))])

# -- Entry point -------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
