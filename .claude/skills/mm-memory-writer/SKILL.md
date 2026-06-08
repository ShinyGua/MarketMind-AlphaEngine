---
name: mm-memory-writer
description: Extracts and stores episodic, semantic, and procedural memories from completed pipeline runs via MCP memory server
user-invocable: false
disable-model-invocation: true
context: fork
agent: mm-light
allowed-tools: Read, Bash, Glob, Grep, mcp__memory__store_memory, mcp__memory__search_memory, mcp__memory__update_memory
---

# Role: Memory Writer

Write memories from a completed pipeline run for long-term recall across future analyses. All memory storage goes through the **memory MCP server** (`mcp__memory__*` tools) for schema validation, ID generation, and index management.

**PYTHON**: Always use `.venv/bin/python3` for all Bash Python commands. Never use bare `python3`.

Workspace path: $ARGUMENTS[0]
Run date: $ARGUMENTS[1] (YYYY-MM-DD)

## Inputs

Read these files from the workspace:
- `{workspace}/decision/{date}/final_decision.json` — investment decision + confidence + reasons
- `{workspace}/reviews/{date}/score_history.json` — review iterations and scores
- `{workspace}/discussion/{date}/thesis_map.json` — consensus, disagreements, key themes
- `{workspace}/profile/company_profile.json` — ticker, sector

**Performance optimization:** Read `{workspace}/shared_context/{date}.json` (contains quant, profile, peers, catalysts in one file) instead of reading each file separately.

**User review (optional):** If `{workspace}/reviews/{date}/user_review.json` exists and `reviewed: true`, the user provided feedback on the report. This MUST be stored as memory.

**Regression flag (optional):** If `{workspace}/eval/{date}/regression_flag.json` exists, a grader caught an error that the reviewer missed. This MUST be stored as a high-importance procedural memory.

## Process

### 1. Create Episodic Memory (always, exactly 1)

Summarize the pipeline run in 2-3 sentences covering:
- Decision (BUY/HOLD/SELL) + confidence + horizon
- Key driver(s) of the decision
- Review history (how many iterations, what failed)
- Debate quality score

Call `mcp__memory__store_memory` with:
- `type`: "episodic"
- `content`: your 2-3 sentence summary
- `tags`: `{ticker, sector, event_type: "daily_analysis", decision, confidence, horizon, key_themes: [...], importance: 0.85}`
- `related_entities`: `[ticker, ...peer tickers from profile]`
- `source_run`: `{run_date: "{date}", workspace: "{workspace}", stage: "reflect"}`

### 2. Extract Semantic Memories (0-3 per run)

From thesis_map.json `consensus` items, extract persistent beliefs about the company or sector. Only create semantic memories for durable knowledge (not transient market conditions).

Good examples (durable): "AMD EPYC server adoption accelerating among hyperscalers"
Bad examples (transient): "AMD stock down 1.92% today" — skip these.

**Supersede check — use store-first-then-update pattern:**

1. First, call `mcp__memory__search_memory` with:
   - `query`: the belief content
   - `filters`: `{type: "semantic", ticker: "{ticker}"}`
   - `limit`: 5

2. If an existing semantic memory conflicts with the new belief, note its `id`.

3. Call `mcp__memory__store_memory` to create the new belief:
   - `type`: "semantic"
   - `content`: the persistent belief
   - `tags`: `{ticker, sector, key_themes: [...], importance: 0.7-0.9}`
   - `related_entities`: relevant tickers
   - `source_run`: `{run_date, workspace, stage: "reflect"}`

4. Read the returned `id` from the store result (e.g., `mem_AMD_2026-03-28_semantic_002`).

5. Call `mcp__memory__update_memory` on the OLD memory with `superseded_by` set to the **new memory's ID** (not a description). This creates a clean audit chain: old → new via stable IDs.

### 3. Extract Procedural Memories (0-2 per run, only if review had failures)

If `score_history.json` shows any failed reviews (pass: false), extract learnings:
- Read `{workspace}/reviews/{date}/revision_briefs/` for blocker details
- For each blocker, create a procedural memory describing:
  - What went wrong (e.g., "MACD direction stated backwards")
  - Why it matters (factuality failure)
  - How to avoid it (always check sign of MACD vs signal before stating direction)

Call `mcp__memory__store_memory` with:
- `type`: "procedural"
- `content`: actionable description of the error pattern and prevention
- `tags`: `{event_type: "review_failure", importance: 0.9}`
- `source_run`: `{run_date, workspace, stage: "reflect"}`

### 4. Store User Review Feedback (if present)

If `{workspace}/reviews/{date}/user_review.json` exists and `reviewed` is true (not skipped):

**4a. Store user feedback as episodic memory:**

Call `mcp__memory__store_memory` with:
- `type`: "episodic"
- `content`: "USER REVIEW for {ticker} {date}: {agrees_with_decision ? 'Agrees' : 'Disagrees'} with {decision}. Feedback: {key_points joined}. Personal context: {personal_context or 'none provided'}"
- `tags`: `{ticker, sector, event_type: "user_review", scope: "user_feedback", decision, importance: 0.95}`
- `related_entities`: `[ticker]`
- `source_run`: `{run_date, workspace, stage: "reflect"}`

**4b. If user disagrees with the decision, store as procedural memory:**

If `agrees_with_decision` is false, call `mcp__memory__store_memory` with:
- `type`: "procedural"
- `content`: "User disagreed with {decision} for {ticker} on {date}. Reason: {key_points}. Consider: {user's alternative view or emphasis}. In future analyses of {ticker}, weight these factors more carefully."
- `tags`: `{ticker, event_type: "user_disagreement", scope: "user_feedback", importance: 0.9}`
- `source_run`: `{run_date, workspace, stage: "reflect"}`

**4c. If user provided personal context, store as semantic user memory:**

If `personal_context` is non-empty, call `mcp__memory__store_memory` with:
- `type`: "semantic"
- `content`: "User context for {ticker}: {personal_context}"
- `tags`: `{ticker, event_type: "user_context", scope: "user_profile", importance: 0.8}`
- `source_run`: `{run_date, workspace, stage: "reflect"}`

This enables future reports to incorporate the user's investment perspective, risk preferences, and domain knowledge. The `scope` tag distinguishes user-level memories (`user_feedback`, `user_profile`) from ticker-level beliefs, preventing them from being confused during retrieval.

### 5. Handle Regression Flag (if present)

If `{workspace}/eval/{date}/regression_flag.json` exists, this means a code-based grader caught an error that the reviewer passed. This is a critical quality gap.

Read the flag file to get the failed grader name and error description, then call `mcp__memory__store_memory` with:
- `type`: "procedural"
- `content`: "REGRESSION: Reviewer passed report but {grader_name} grader caught {error_description}. The reviewer should check {specific_check} in future reviews."
- `tags`: `{event_type: "regression", importance: 1.0}`
- `source_run`: `{run_date, workspace, stage: "reflect"}`

### 6. Summary

After writing all memories, print a summary:
- Episodic memories created: 1
- Semantic memories created: N
- Procedural memories created: N
- User review memories created: N
- Regression memories created: N
- Total memories stored: N

## Output

No file output beyond MCP memory storage. All memories are stored via the memory MCP server, which handles schema validation, ID generation, and index management.

## Quality Rules

- Episodic content must be self-contained — reader understands the run without reading other files
- Semantic content must be belief-like, not event-like
- Procedural content must be actionable — reader knows what to check or avoid
- Importance scoring: 1.0 for regressions, 0.9 for review failures, 0.85 for decisions, 0.7-0.85 for beliefs
- Always include ticker and sector in tags for retrieval
- Never fabricate — only extract from actual artifacts
- Use MCP tools for ALL memory operations — do not directly write to JSONL files
