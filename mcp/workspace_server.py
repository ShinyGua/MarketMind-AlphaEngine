"""
MCP server for MarketMind workspace artifact CRUD and state management.
Exposes workspace data as MCP resources and provides tools for creating
workspaces, writing artifacts, and updating pipeline status.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared.contracts import (  # noqa: E402
    STAGES,
    evidence_digest_path, shared_context_read_path, quant_summary_path,
    valuation_summary_path,
    thesis_map_path, decision_path, memory_context_read_path, detect_final_report,
)

import mcp.types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
WORKSPACES_DIR = PROJECT_ROOT / os.environ.get("WORKSPACES_DIR", "workspaces")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_under(base: Path, full: Path) -> Path:
    """Resolve *full* and verify it lives under *base*."""
    resolved = full.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path traversal blocked: {resolved} is not under {base}")
    return resolved

def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _ticker_dirs() -> list[str]:
    """Return sorted ticker directory names (skip 'shared' and hidden dirs)."""
    if not WORKSPACES_DIR.is_dir():
        return []
    return sorted(
        d.name for d in WORKSPACES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "shared"
    )

def _dates_for_ticker(ticker: str) -> list[str]:
    """Return sorted YYYY-MM-DD dates present under any subdirectory."""
    base = WORKSPACES_DIR / ticker
    dates: set[str] = set()
    for sub in ("raw", "normalized", "quant", "valuation", "discussion", "drafts",
                "reviews", "decision", "final", "exports", "eval"):
        d = base / sub
        if d.is_dir():
            dates.update(c.name for c in d.iterdir() if c.is_dir() and DATE_RE.match(c.name))
    # New layout: shared_context/{date}.json
    sc_dir = base / "shared_context"
    if sc_dir.is_dir():
        for child in sc_dir.iterdir():
            if child.is_file() and child.suffix == ".json" and DATE_RE.match(child.stem):
                dates.add(child.stem)
    # Legacy layout: {date}_shared_context.json files at ticker root
    if base.is_dir():
        for child in base.iterdir():
            if child.is_file() and child.name.endswith("_shared_context.json"):
                dp = child.name.replace("_shared_context.json", "")
                if DATE_RE.match(dp):
                    dates.add(dp)
    return sorted(dates)

def _read_file_text(path: Path) -> str | None:
    """Read file as text, return None if missing."""
    return path.read_text(encoding="utf-8") if path.is_file() else None

def _read_file_json(path: Path):
    """Read and parse JSON file, return None if missing."""
    text = _read_file_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

def _find_latest_draft(ws: Path, date: str) -> tuple[str | None, str | None]:
    """Find highest-version draft in drafts/{date}/. Returns (text, version)."""
    drafts_dir = ws / "drafts" / date
    if not drafts_dir.is_dir():
        return None, None
    best_v, best_path = -1, None
    for f in drafts_dir.glob("*_v*.md"):
        m = re.search(r"_v(\d+)\.md$", f.name)
        if m and int(m.group(1)) > best_v:
            best_v = int(m.group(1))
            best_path = f
    if best_path is None:
        return None, None
    return best_path.read_text(encoding="utf-8"), f"v{best_v}"

# Resource URI -> disk path mapping for date-stamped artifacts
_DATE_ARTIFACT_MAP = {
    "shared_context.json":  lambda t, d: shared_context_read_path(WORKSPACES_DIR / t, d),
    "evidence_digest.json": lambda t, d: evidence_digest_path(WORKSPACES_DIR / t, d),
    "quant_summary.json":   lambda t, d: quant_summary_path(WORKSPACES_DIR / t, d),
    "valuation_summary.json": lambda t, d: valuation_summary_path(WORKSPACES_DIR / t, d),
    "thesis_map.json":      lambda t, d: thesis_map_path(WORKSPACES_DIR / t, d),
    "final_decision.json":  lambda t, d: decision_path(WORKSPACES_DIR / t, d),
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
server = Server("mm-workspace")

# ---- Tools ----------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[mcp.types.Tool]:
    return [
        mcp.types.Tool(
            name="write_artifact",
            description="Write a file into a ticker workspace with path-traversal protection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker":       {"type": "string", "description": "Ticker symbol (e.g. NVDA)"},
                    "path":         {"type": "string", "description": "Relative path within workspace"},
                    "content":      {"type": "string", "description": "File content to write"},
                    "content_type": {"type": "string", "enum": ["json", "markdown", "csv", "text"],
                                     "default": "json", "description": "Content type hint"},
                },
                "required": ["ticker", "path", "content"],
            },
        ),
        mcp.types.Tool(
            name="update_status",
            description="Update pipeline status.json for a ticker workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker":          {"type": "string"},
                    "stage":           {"type": "string", "description": "Current pipeline stage"},
                    "completed_stage": {"type": "string", "description": "Stage to mark completed"},
                    "run_date":        {"type": "string", "description": "YYYY-MM-DD run date"},
                    "error":           {"type": "string", "description": "Error message to append"},
                    "progress_mode":   {"type": "string", "description": "TodoWrite owner: 'monitor' or 'orchestrator'"},
                },
                "required": ["ticker"],
            },
        ),
        mcp.types.Tool(
            name="create_workspace",
            description="Create a new ticker workspace with skeleton directories and initial status.json.",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Ticker symbol"}},
                "required": ["ticker"],
            },
        ),
        mcp.types.Tool(
            name="create_date_dirs",
            description="Create all date-stamped subdirectories for a ticker workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol"},
                    "date":   {"type": "string", "description": "Date in YYYY-MM-DD format"},
                },
                "required": ["ticker", "date"],
            },
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]:
    try:
        dispatch = {
            "write_artifact":   _tool_write_artifact,
            "update_status":    _tool_update_status,
            "create_workspace": _tool_create_workspace,
            "create_date_dirs": _tool_create_date_dirs,
        }
        handler = dispatch.get(name)
        result = handler(arguments) if handler else {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc)}
    return [mcp.types.TextContent(type="text", text=json.dumps(result, indent=2))]

# -- tool implementations --------------------------------------------------

def _tool_write_artifact(args: dict) -> dict:
    ticker, rel_path, content = args["ticker"], args["path"], args["content"]
    workspace = WORKSPACES_DIR / ticker
    full_path = _ensure_under(workspace, workspace / rel_path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {"written": True, "path": str(full_path), "size_bytes": full_path.stat().st_size}

def _tool_update_status(args: dict) -> dict:
    ticker = args["ticker"]
    status_path = _ensure_under(WORKSPACES_DIR, WORKSPACES_DIR / ticker / "status.json")
    status = _read_json(status_path) if status_path.exists() else {
        "stage": "pending", "ticker": ticker, "stages_completed": [], "errors": [],
    }
    if args.get("stage") is not None:
        status["stage"] = args["stage"]
    if args.get("completed_stage") is not None:
        cs = args["completed_stage"]
        if cs not in STAGES:
            return {"error": f"Invalid stage name '{cs}'. Must be one of: {STAGES}"}
        completed = status.setdefault("stages_completed", [])
        if cs not in completed:
            completed.append(cs)
    if args.get("run_date") is not None:
        status["run_date"] = args["run_date"]
    if args.get("progress_mode") is not None:
        pm = args["progress_mode"]
        if pm not in ("monitor", "orchestrator"):
            return {"error": f"Invalid progress_mode '{pm}'. Must be 'monitor' or 'orchestrator'"}
        status["progress_mode"] = pm
    if args.get("error") is not None:
        status.setdefault("errors", []).append(args["error"])
    status["updated_at"] = _now_iso()
    _write_json(status_path, status)
    return status

def _tool_create_workspace(args: dict) -> dict:
    ticker = args["ticker"]
    workspace = WORKSPACES_DIR / ticker
    _ensure_under(WORKSPACES_DIR, workspace)
    (workspace / "profile").mkdir(parents=True, exist_ok=True)
    status_path = workspace / "status.json"
    if not status_path.exists():
        _write_json(status_path, {
            "stage": "pending", "ticker": ticker, "stages_completed": [], "errors": [],
        })
    return {"created": True, "workspace": str(workspace.relative_to(PROJECT_ROOT))}

def _tool_create_date_dirs(args: dict) -> dict:
    ticker, date = args["ticker"], args["date"]
    if not DATE_RE.match(date):
        raise ValueError(f"Invalid date format: {date!r} (expected YYYY-MM-DD)")
    workspace = WORKSPACES_DIR / ticker
    _ensure_under(WORKSPACES_DIR, workspace)
    dirs = [
        f"raw/{date}/news", f"raw/{date}/filings", f"raw/{date}/prices",
        f"raw/{date}/ownership", f"raw/{date}/calendar",
        f"raw/{date}/fundamentals/peers",
        f"normalized/{date}/evidence_cards", f"normalized/{date}/time_series",
        f"normalized/{date}/tables", f"quant/{date}", f"valuation/{date}",
        f"discussion/{date}/analyst_memos", f"discussion/{date}/panel/round_1",
        f"drafts/{date}", f"reviews/{date}/final_reviews",
        f"reviews/{date}/revision_briefs", f"decision/{date}", f"final/{date}",
        f"exports/{date}/pdf/charts", f"exports/{date}/web", f"eval/{date}",
        "memory", "shared_context",
    ]
    for d in dirs:
        (workspace / d).mkdir(parents=True, exist_ok=True)
    return {"created": True, "date": date, "dirs_count": len(dirs)}

# ---- Resources ------------------------------------------------------------

@server.list_resources()
async def list_resources() -> list[mcp.types.Resource]:
    resources: list[mcp.types.Resource] = [
        mcp.types.Resource(
            uri="workspace://list", name="All workspaces",
            description="JSON list of all ticker workspaces", mimeType="application/json",
        )
    ]
    _UNDATED = {
        "profile/company_profile.json": "Company profile",
        "profile/peer_set.json":        "Peer set",
        "resolved_config.json":         "Resolved config",
        "status.json":                  "Pipeline status",
    }
    for ticker in _ticker_dirs():
        for rel, desc in _UNDATED.items():
            if (WORKSPACES_DIR / ticker / rel).exists():
                resources.append(mcp.types.Resource(
                    uri=f"workspace://{ticker}/{rel}",
                    name=f"{ticker} -- {desc}", mimeType="application/json",
                ))
        for date in _dates_for_ticker(ticker):
            for artifact, path_fn in _DATE_ARTIFACT_MAP.items():
                if path_fn(ticker, date).exists():
                    resources.append(mcp.types.Resource(
                        uri=f"workspace://{ticker}/{date}/{artifact}",
                        name=f"{ticker} {date} -- {artifact}", mimeType="application/json",
                    ))
            # Composite resource packets
            resources.append(mcp.types.Resource(
                uri=f"workspace://{ticker}/{date}/draft_packet",
                name=f"{ticker} {date} -- draft_packet",
                description="Bundled writer input: evidence + context + thesis + debate + memory",
            ))
            resources.append(mcp.types.Resource(
                uri=f"workspace://{ticker}/{date}/review_packet",
                name=f"{ticker} {date} -- review_packet",
                description="Bundled reviewer input: latest draft + evidence + context + thesis + memory",
            ))
            # Per-role memory context
            ws = WORKSPACES_DIR / ticker
            for role in ("analyst", "writer", "reviewer"):
                mc = memory_context_read_path(ws, date, role)
                if mc.is_file():
                    resources.append(mcp.types.Resource(
                        uri=f"workspace://{ticker}/{date}/memory_context/{role}",
                        name=f"{ticker} {date} -- memory_context/{role}",
                    ))
    return resources

@server.read_resource()
async def read_resource(uri: str) -> str:
    try:
        return _read_resource_impl(uri)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)

def _read_resource_impl(uri: str) -> str:
    if not uri.startswith("workspace://"):
        raise ValueError(f"Unsupported URI scheme: {uri}")
    path_part = uri[len("workspace://"):]

    if path_part == "list":
        return json.dumps(_ticker_dirs(), indent=2)

    segments = path_part.split("/", maxsplit=1)
    if len(segments) < 2:
        raise ValueError(f"Invalid resource URI: {uri}")
    ticker, remainder = segments

    # Date-stamped artifact: {date}/{artifact_name}
    m = re.match(r"^(\d{4}-\d{2}-\d{2})/(.+)$", remainder)
    if m:
        date_str, artifact = m.group(1), m.group(2)
        ws = WORKSPACES_DIR / ticker

        # Composite resource: draft_packet
        if artifact == "draft_packet":
            packet = {
                "evidence_digest": _read_file_json(evidence_digest_path(ws, date_str)),
                "shared_context": _read_file_json(shared_context_read_path(ws, date_str)),
                "thesis_map": _read_file_json(thesis_map_path(ws, date_str)),
                "debate_summary": _read_file_text(ws / "discussion" / date_str / "debate_summary.md"),
                "memory_context": _read_file_json(memory_context_read_path(ws, date_str, "writer")),
            }
            return json.dumps(packet, indent=2, ensure_ascii=False)

        # Composite resource: review_packet
        if artifact == "review_packet":
            draft_text, draft_version = _find_latest_draft(ws, date_str)
            packet = {
                "latest_draft": draft_text,
                "draft_version": draft_version,
                "evidence_digest": _read_file_json(evidence_digest_path(ws, date_str)),
                "shared_context": _read_file_json(shared_context_read_path(ws, date_str)),
                "thesis_map": _read_file_json(thesis_map_path(ws, date_str)),
                "memory_context": _read_file_json(memory_context_read_path(ws, date_str, "reviewer")),
            }
            return json.dumps(packet, indent=2, ensure_ascii=False)

        # Per-role memory context
        if artifact.startswith("memory_context/"):
            role = artifact.split("/", 1)[1]
            mc = memory_context_read_path(ws, date_str, role)
            if mc.is_file():
                return mc.read_text(encoding="utf-8")
            return json.dumps(None)

        if artifact not in _DATE_ARTIFACT_MAP:
            raise ValueError(f"Unknown date artifact: {artifact}")
        file_path = _DATE_ARTIFACT_MAP[artifact](ticker, date_str)
    else:
        file_path = WORKSPACES_DIR / ticker / remainder

    file_path = _ensure_under(WORKSPACES_DIR, file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Resource not found: {file_path}")
    return file_path.read_text(encoding="utf-8")

# ---- Prompts --------------------------------------------------------------

@server.list_prompts()
async def list_prompts() -> list[mcp.types.Prompt]:
    return [
        mcp.types.Prompt(
            name="pipeline_status_summary",
            description="Formatted summary of pipeline status across all workspaces.",
            arguments=[],
        ),
        mcp.types.Prompt(
            name="workspace_overview",
            description="List all dates and artifacts for one or all workspaces.",
            arguments=[
                mcp.types.PromptArgument(
                    name="ticker",
                    description="Ticker to inspect (omit for all workspaces)",
                    required=False,
                ),
            ],
        ),
        mcp.types.Prompt(
            name="report_writer",
            description="Report writing template with section structure and rules.",
            arguments=[
                mcp.types.PromptArgument(name="ticker", description="Company ticker", required=True),
                mcp.types.PromptArgument(name="date", description="Run date YYYY-MM-DD", required=True),
                mcp.types.PromptArgument(name="mode", description="initial or revision", required=True),
            ],
        ),
        mcp.types.Prompt(
            name="report_reviewer",
            description="Review scoring rubric with dimensions, blockers, and thresholds.",
            arguments=[
                mcp.types.PromptArgument(name="ticker", description="Company ticker", required=True),
                mcp.types.PromptArgument(name="date", description="Run date YYYY-MM-DD", required=True),
            ],
        ),
    ]

@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> mcp.types.GetPromptResult:
    arguments = arguments or {}
    try:
        if name == "pipeline_status_summary":
            text = _prompt_pipeline_status()
        elif name == "workspace_overview":
            text = _prompt_workspace_overview(arguments.get("ticker"))
        elif name == "report_writer":
            text = _prompt_report_writer(arguments.get("ticker", ""), arguments.get("date", ""), arguments.get("mode", "initial"))
        elif name == "report_reviewer":
            text = _prompt_report_reviewer(arguments.get("ticker", ""), arguments.get("date", ""))
        else:
            text = f"Unknown prompt: {name}"
    except Exception as exc:
        text = f"Error generating prompt: {exc}"
    return mcp.types.GetPromptResult(
        description=f"Result for prompt '{name}'",
        messages=[mcp.types.PromptMessage(
            role="user",
            content=mcp.types.TextContent(type="text", text=text),
        )],
    )

def _prompt_pipeline_status() -> str:
    tickers = _ticker_dirs()
    if not tickers:
        return "No workspaces found."
    lines = ["# Pipeline Status Summary", ""]
    for ticker in tickers:
        s = _read_json(WORKSPACES_DIR / ticker / "status.json")
        if not s:
            lines += [f"## {ticker}  --  (no status.json)", ""]
            continue
        completed = s.get("stages_completed", [])
        errors = s.get("errors", [])
        lines.append(f"## {ticker}")
        lines.append(f"- **Current stage**: {s.get('stage', 'unknown')}")
        lines.append(f"- **Run date**: {s.get('run_date', '--')}")
        lines.append(f"- **Completed**: {', '.join(completed) if completed else 'none'}")
        lines.append(f"- **Errors**: {len(errors)}")
        for err in errors[-3:]:
            lines.append(f"  - {err}")
        lines.append(f"- **Last updated**: {s.get('updated_at', '--')}")
        lines.append("")
    return "\n".join(lines)

def _prompt_workspace_overview(ticker: str | None) -> str:
    tickers = [ticker] if ticker else _ticker_dirs()
    if not tickers:
        return "No workspaces found."
    lines = ["# Workspace Overview", ""]
    for t in tickers:
        ws = WORKSPACES_DIR / t
        if not ws.is_dir():
            lines += [f"## {t}  --  directory not found", ""]
            continue
        lines.append(f"## {t}")
        for rel in ("config.yaml", "resolved_config.json", "status.json",
                     "profile/company_profile.json", "profile/peer_set.json"):
            fp = ws / rel
            if fp.exists():
                lines.append(f"  - `{rel}` ({fp.stat().st_size:,} bytes)")
        dates = _dates_for_ticker(t)
        if not dates:
            lines.append("  - No date-stamped data yet.")
        for date in dates:
            lines.append(f"  ### {date}")
            for artifact, path_fn in _DATE_ARTIFACT_MAP.items():
                fp = path_fn(t, date)
                if fp.exists():
                    lines.append(f"    - `{artifact}` ({fp.stat().st_size:,} bytes)")
            for sub in ("discussion", "drafts", "final"):
                d = ws / sub / date
                if d.is_dir():
                    for f in sorted(d.rglob("*")):
                        if f.is_file():
                            lines.append(f"    - `{f.relative_to(ws)}`")
        lines.append("")
    return "\n".join(lines)

def _prompt_report_writer(ticker: str, date: str, mode: str) -> str:
    if mode == "revision":
        return (
            f"# Report Revision Instructions — {ticker} {date}\n\n"
            f"Read `workspace://{ticker}/{date}/review_packet` for the current draft and review context.\n"
            f"Read `reviews/{date}/revision_briefs/revision_brief.json` for specific fixes.\n\n"
            "## Rules\n"
            "- Rewrite ONLY the sections specified in the revision brief\n"
            "- Do NOT rewrite sections that passed review\n"
            "- Increment version number (e.g., daily_v1.md → daily_v2.md)\n"
            f"- Write to `drafts/{date}/`\n"
        )
    return (
        f"# Daily Report Template — {ticker} {date}\n\n"
        f"Read `workspace://{ticker}/{date}/draft_packet` for all input context.\n\n"
        "## Section Structure\n\n"
        "1. **Executive Summary** — 3-5 bullets, lead with most material event\n"
        "2. **Market Context** — Macro, index performance, relative strength\n"
        "3. **Company Events & News** — Ranked by materiality, reference evidence card IDs\n"
        "4. **Price Action & Technical Snapshot** — Price, returns, RSI, MACD, SMA, key levels\n"
        "5. **Sector & Peers** — Relative performance, notable peer developments\n"
        "6. **Catalysts & Risks** — Upcoming events with dates, bull/bear from thesis_map\n"
        "7. **Investment View** — Synthesized thesis, confidence, time horizon\n"
        "8. **Sources & Evidence** — Key evidence card IDs with source names\n\n"
        "## Writing Rules\n"
        "- Every material claim must reference a specific evidence card ID or quant data point\n"
        "- Separate facts from interpretation\n"
        "- Company-specific, not generic market commentary\n"
        "- Include both bull and bear perspectives from thesis_map.writer_guidance\n"
        "- State uncertainty explicitly when confidence is limited\n"
        f"\nWrite to: `drafts/{date}/daily_v1.md`"
    )


def _prompt_report_reviewer(ticker: str, date: str) -> str:
    return (
        f"# Review Scoring Rubric — {ticker} {date}\n\n"
        f"Read `workspace://{ticker}/{date}/review_packet` for the draft and all context.\n\n"
        "## Scoring Dimensions (1-10 scale)\n\n"
        "| Dimension | Weight | Description |\n"
        "|-----------|--------|-------------|\n"
        "| factuality | critical | Facts match evidence cards + quant data |\n"
        "| evidence_coverage | high | All material cards (≥0.7) addressed |\n"
        "| decision_quality | high | Thesis follows logically from evidence |\n"
        "| market_context | medium | Macro properly framed |\n"
        "| company_specificity | medium | Company-focused, not generic |\n"
        "| risk_discipline | medium | Specific risks, no overconfidence |\n\n"
        "## Blocker Conditions (immediate fail)\n"
        "- Factual claim with zero evidence backing\n"
        "- Quant number contradicts quant_summary.json\n"
        "- Wrong time window referenced\n"
        "- Major high-materiality event completely omitted\n"
        "- Investment view contradicts own evidence\n\n"
        "## Thresholds\n"
        "- Overall score >= 8.0\n"
        "- Factuality >= 9.0\n"
        "- Any blocker → immediate fail regardless of scores\n\n"
        "## Output Format\n"
        f"Write review to: `reviews/{date}/final_reviews/review_v{{N}}.json`\n"
        f"If failed, write revision brief to: `reviews/{date}/revision_briefs/revision_brief.json`\n"
        f"Append to score history: `reviews/{date}/score_history.json`\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
