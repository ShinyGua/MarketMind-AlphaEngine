# /mm:run — Run the full research pipeline

## Description

Execute the complete MarketMind equity research pipeline for a company workspace. Runs all 12 stages autonomously from start to finish, producing a final research report with BUY/HOLD/SELL decision.

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
python3 -c "
import os, json, datetime
workspace = 'WORKSPACE_PATH'
today = datetime.date.today().isoformat()
mode = 'daily'  # from status.json
report_path = os.path.join(workspace, 'final', today, f'{mode}_report.md')
print(json.dumps({'exists': os.path.exists(report_path), 'date': today}))
"
    print(json.dumps({'exists': False, 'is_today': False}))
"
```

- **Report exists for today** → Output: "Today's report for {TICKER} already exists. Re-run? (yes/no)" Wait for chat reply.
- **No report for today** → Auto-reset: set `stages_completed: []`, `stage: "initialized"`, `run_date: "{today}"`. Output: "No report for today. Starting fresh run..."

**If `stage != "completed"` and `run_date` matches today:**
Output: "Resuming {TICKER} pipeline from {next_stage}..."

**If `stage != "completed"` and `run_date` is a previous day:**
Auto-reset: set `stages_completed: []`, `stage: "initialized"`, `run_date: "{today}"`. Output: "Previous run was from {run_date}. Starting fresh for today..."

### 3. Launch Background Progress Monitor

Launch the **mm-progress-monitor** skill as a background agent:

```
Agent tool:
  description: "Pipeline progress monitor"
  prompt: "Read and follow .claude/skills/mm-progress-monitor/SKILL.md for workspace {WORKSPACE_PATH}"
  run_in_background: true
```

This monitor will read `status.json` periodically and update the TodoWrite checklist. It runs independently and does not block the pipeline.

### 4. Execute Pipeline Directly

**Do NOT use ralph-loop.** Execute the pipeline yourself by following the orchestrator protocol directly.

1. Read `.claude/skills/mm-orchestrator/SKILL.md` — this is your complete instruction set
2. Follow its IRON LAW and Execution Protocol for workspace `{WORKSPACE_PATH}`
3. Use `.venv/bin/python3` for all Python commands
4. Execute ALL 12 stages sequentially without stopping
5. After each stage: update `{WORKSPACE_PATH}/status.json`, then immediately do the next stage
6. Dispatch sub-skills via Agent tool (for parallel stages like collect and discuss_memos)

**You are now the orchestrator. Start executing stages immediately. Do not delegate to another tool or skill — do it yourself right here.**

### 5. Completion

When the pipeline finishes:

Read `{workspace}/decision/final_decision.json` and display:

```
Pipeline complete for {TICKER}

  Decision:   {BUY|HOLD|SELL} (confidence: {score})
  Report:     {workspace}/final/daily_report.md
  Decision:   {workspace}/decision/final_decision.json
```
