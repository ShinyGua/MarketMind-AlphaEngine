#!/usr/bin/env python3
"""Cost tracker: estimate token consumption and cost per pipeline run."""

import sys
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Stage directory name -> pipeline stage mapping
DIR_TO_STAGE = {
    "raw": "collect",
    "normalized": "normalize",
    "quant": "quant",
    "discussion": "discuss",
    "drafts": "draft",
    "reviews": "review",
    "decision": "decide",
    "final": "export",
}

# Stage -> model tier mapping
STAGE_TO_TIER = {
    "collect": "sonnet",
    "normalize": "sonnet",
    "quant": "sonnet",
    "discuss": "opus",
    "draft": "opus",
    "review": "opus",
    "decide": "opus",
    "export": "opus",
}

# Pricing per 1K tokens (USD)
PRICING = {
    "opus": {"input": 0.005, "output": 0.025},
    "sonnet": {"input": 0.003, "output": 0.015},
}


def estimate_tokens(char_count: int) -> int:
    """Estimate tokens from character count (approx 4 chars per token)."""
    return max(1, char_count // 4)


def walk_date_files(workspace: Path, date: str) -> dict[str, list[dict]]:
    """Walk workspace directories under date folders and group files by stage.

    Returns {stage: [{path, chars, tokens}]}.
    """
    stage_files: dict[str, list[dict]] = {}

    for dir_name, stage_name in DIR_TO_STAGE.items():
        date_dir = workspace / dir_name / date
        if not date_dir.exists():
            continue

        files = []
        for root, _dirs, filenames in os.walk(date_dir):
            for fname in filenames:
                fpath = Path(root) / fname
                try:
                    char_count = len(fpath.read_bytes())
                    tokens = estimate_tokens(char_count)
                    files.append({
                        "path": str(fpath.relative_to(workspace)),
                        "chars": char_count,
                        "tokens": tokens,
                    })
                except (OSError, PermissionError):
                    continue

        if files:
            stage_files[stage_name] = files

    # Also check shared market context if linked
    shared_dir = workspace.parent / "shared" / "market_context" / date
    if shared_dir.exists():
        files = []
        for root, _dirs, filenames in os.walk(shared_dir):
            for fname in filenames:
                fpath = Path(root) / fname
                try:
                    char_count = len(fpath.read_bytes())
                    tokens = estimate_tokens(char_count)
                    files.append({
                        "path": str(fpath),
                        "chars": char_count,
                        "tokens": tokens,
                    })
                except (OSError, PermissionError):
                    continue
        if files:
            stage_files.setdefault("collect", []).extend(files)

    return stage_files


def compute_costs(stage_files: dict[str, list[dict]]) -> dict:
    """Compute estimated costs for each stage.

    For simplicity:
    - Files in a stage directory are counted as output tokens for that stage.
    - They are also counted as input tokens for the next downstream stage(s).
    """
    stage_order = ["collect", "normalize", "quant", "discuss", "draft", "review", "decide", "export"]

    stage_breakdown = {}
    total_input = 0
    total_output = 0
    total_cost = 0.0

    for stage in stage_order:
        files = stage_files.get(stage, [])
        output_tokens = sum(f["tokens"] for f in files)

        # Input tokens: sum of all upstream stage outputs that this stage reads
        # Simplified: each stage reads its own files as output, and they become
        # input to downstream stages. For cost estimation, we count the stage's
        # own output as its output cost, and treat it as input for the next stage.
        # For the stage itself, its input is the sum of all previous stages' outputs.
        input_tokens = 0
        stage_idx = stage_order.index(stage) if stage in stage_order else -1
        if stage_idx > 0:
            for prev_stage in stage_order[:stage_idx]:
                prev_files = stage_files.get(prev_stage, [])
                input_tokens += sum(f["tokens"] for f in prev_files)

        tier = STAGE_TO_TIER.get(stage, "opus")
        pricing = PRICING[tier]

        input_cost = (input_tokens / 1000) * pricing["input"]
        output_cost = (output_tokens / 1000) * pricing["output"]
        stage_cost = input_cost + output_cost

        stage_breakdown[stage] = {
            "est_input_tokens": input_tokens,
            "est_output_tokens": output_tokens,
            "est_tokens": input_tokens + output_tokens,
            "model_tier": tier,
            "est_cost": round(stage_cost, 4),
        }

        total_input += input_tokens
        total_output += output_tokens
        total_cost += stage_cost

    return {
        "estimated_input_tokens": total_input,
        "estimated_output_tokens": total_output,
        "estimated_total_tokens": total_input + total_output,
        "estimated_cost_usd": round(total_cost, 4),
        "stage_breakdown": stage_breakdown,
    }


def grade(workspace: str, date: str) -> dict:
    ws = Path(workspace)

    result = {
        "grader": "cost",
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "stage_breakdown": {},
        "errors": [],
    }

    if not ws.exists():
        result["errors"].append(f"Workspace not found: {ws}")
        return result

    stage_files = walk_date_files(ws, date)

    if not stage_files:
        result["errors"].append(f"No date-stamped files found for {date} in {ws}")
        return result

    costs = compute_costs(stage_files)
    result.update(costs)

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
    out_dir = ws / "eval" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cost_result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
