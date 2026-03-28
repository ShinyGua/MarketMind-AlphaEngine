# /mm:run — Run the full research pipeline

## Description

Execute the complete MarketMind equity research pipeline for a company workspace. Runs 14 stages mostly autonomously, with a single pause at stage 13 (`user_review`) to collect user feedback. Produces a final research report with BUY/HOLD/SELL decision, then runs evaluation and long-term memory updates.

## Arguments

- `$ARGUMENTS[0]`: Workspace path (e.g., `workspaces/NVDA`)

If no argument provided, list available workspaces and ask user to choose.

## Steps

### 1. Validate Workspace

```bash
MM_ROOT=$(pwd)
```

Check that `$ARGUMENTS[0]` exists and contains `config.yaml` and `status.json`. If not, display error and suggest running `/mm:init` first.

### 2. Smart Re-run Check

Determine today's date: `python3 -c "import datetime; print(datetime.date.today().isoformat())"`

Read `{workspace}/status.json`. Determine the ticker and run_mode.

**If `stage == "completed"`:**

Check if today's date folder already has a report:

```bash
.venv/bin/python3 -c "
import os, json, datetime
workspace = 'WORKSPACE_PATH'
today = datetime.date.today().isoformat()
# Read run_mode from status.json
status = json.load(open(os.path.join(workspace, 'status.json'))) if os.path.exists(os.path.join(workspace, 'status.json')) else {}
mode = status.get('run_mode', 'daily')
basename = 'weekly_report' if mode == 'weekly' else 'daily_report'
report_path = os.path.join(workspace, 'final', today, f'{basename}.md')
print(json.dumps({'exists': os.path.exists(report_path), 'date': today, 'mode': mode}))
"
```

- **Report exists for today** → Output: "Today's report for {TICKER} already exists. Re-run? (yes/no)" Wait for chat reply.
- **No report for today** → Auto-reset: set `stages_completed: []`, `stage: "initialized"`, `run_date: "{today}"`. Output: "No report for today. Starting fresh run..."

**If `stage != "completed"` and `run_date` matches today:**
Output: "Resuming {TICKER} pipeline from {next_stage}..."

**If `stage != "completed"` and `run_date` is a previous day:**
Auto-reset: set `stages_completed: []`, `stage: "initialized"`, `run_date: "{today}"`. Output: "Previous run was from {run_date}. Starting fresh for today..."

### 3. Launch Background Progress Monitor

**This step is MANDATORY. Do NOT skip it. Do NOT proceed to Step 4 until the monitor launch is attempted.**

Launch the **mm-progress-monitor** as a background agent:

```
Agent tool:
  description: "Pipeline progress monitor"
  prompt: "Read and follow .claude/skills/mm-progress-monitor/SKILL.md for workspace {WORKSPACE_PATH}"
  run_in_background: true
```

**After launching the monitor**, set `progress_mode` in `status.json` via the MCP tool:

```
mcp__workspace__update_status with:
  ticker: {TICKER}
  progress_mode: "monitor"
```

The monitor reads `status.json` every 5 seconds and updates the TodoWrite checklist. It runs independently and does not block the pipeline. **The monitor is the sole TodoWrite owner in this mode.**

**If the Agent tool call fails** (monitor does not launch):

Set `progress_mode` to `"orchestrator"` instead:

```
mcp__workspace__update_status with:
  ticker: {TICKER}
  progress_mode: "orchestrator"
```

In this fallback mode, the orchestrator itself will write TodoWrite after each stage transition. The user must always see progress regardless of whether the monitor is running.

### 4. Execute Pipeline Directly

**Do NOT use ralph-loop.** Execute the pipeline yourself by following the orchestrator protocol directly.

1. Read `.claude/skills/mm-orchestrator/SKILL.md` — this is your complete instruction set
2. Follow its IRON LAW and Execution Protocol for workspace `{WORKSPACE_PATH}`
3. Use `.venv/bin/python3` for all Python commands
4. Execute ALL 14 stages sequentially (stage 13 pauses for user review; all others run autonomously)
5. After each stage: update `{WORKSPACE_PATH}/status.json`, then immediately do the next stage
6. Dispatch sub-skills via Agent tool (for parallel stages like collect and discuss_memos)

**You are now the orchestrator. Start executing stages immediately. Do not delegate to another tool or skill — do it yourself right here.**

**Note:** The progress monitor (or orchestrator fallback) handles TodoWrite updates. Do NOT call TodoWrite yourself in this step unless `progress_mode` is `"orchestrator"` — in that case, follow the orchestrator SKILL.md instructions for self-reporting.

### 5. Completion

When the pipeline finishes:

Read `{workspace}/decision/{date}/final_decision.json` and `{workspace}/eval/{date}/release_gate.json`, then display:

```
Pipeline complete for {TICKER} ({date})

  Decision:   {BUY|HOLD|SELL} (confidence: {score})
  Report:     {workspace}/final/{date}/{daily_report|weekly_report}.md
  PDF:        {workspace}/exports/{date}/pdf/report.pdf
  Decision:   {workspace}/decision/{date}/final_decision.json
  Release:    {release_status}
```

(Use `daily_report.md` or `weekly_report.md` based on `resolved_config.run_mode`)
