# MarketMind Skill Index For Codex

Read the relevant `.claude/skills/*/SKILL.md` file on demand. Do not bulk-load
all skills at session start.

**Invocation policy:** only `mm-init` and `mm-orchestrator` are implicitly
invocable entry points. Every stage skill below is `allow_implicit_invocation:
false` — orchestrated by `mm-orchestrator`, not run standalone. Invoke one
explicitly (`$mm-…`) only for targeted debugging.

## User-Invocable Workflows

| Request | Command file | Skill files |
| --- | --- | --- |
| Initialize workspace | `plugin/commands/init.md` | `.claude/skills/mm-init/SKILL.md`, `.claude/skills/mm-company-resolver/SKILL.md` |
| Run pipeline | `plugin/commands/run.md` | `.claude/skills/mm-orchestrator/SKILL.md`, then stage skills as needed |
| Show status | `plugin/commands/status.md` | `.claude/skills/mm-progress-monitor/SKILL.md` only for progress logic |
| Launch dashboard | `plugin/commands/mm-dashboard.md` | none |

## Pipeline Stages

| Stage | Primary skill files |
| --- | --- |
| `resolve_config` | `.claude/skills/mm-orchestrator/SKILL.md` |
| `init_workspace` | `.claude/skills/mm-company-resolver/SKILL.md` |
| `collect` | `.claude/skills/mm-market-desk/SKILL.md`, `.claude/skills/mm-company-desk/SKILL.md`, `.claude/skills/mm-sector-desk/SKILL.md`, `.claude/skills/mm-web-research/SKILL.md` |
| `normalize` | `.claude/skills/mm-orchestrator/SKILL.md` |
| `quant` | `.claude/skills/mm-quant-analyst/SKILL.md` |
| `valuation` | `.claude/skills/mm-valuation-engine/SKILL.md` |
| `discuss_memos` | `.claude/skills/mm-market-analyst/SKILL.md`, `.claude/skills/mm-company-analyst/SKILL.md`, `.claude/skills/mm-risk-analyst/SKILL.md`, optional `.claude/skills/mm-valuation-analyst/SKILL.md`, `.claude/skills/mm-chips-analyst/SKILL.md`, `.claude/skills/mm-catalyst-analyst/SKILL.md` |
| `discuss_debate` | `.claude/skills/mm-discussion-panelist/SKILL.md` (per-round views), `.claude/skills/mm-discussion-moderator/SKILL.md` (tally) |
| `discuss_synthesis` | `.claude/skills/mm-discussion-moderator/SKILL.md` |
| `draft` | `.claude/skills/mm-report-writer/SKILL.md` |
| `review` | `.claude/skills/mm-report-reviewer/SKILL.md` |
| `decide` | `.claude/skills/mm-decision-panelist/SKILL.md` (per-round ballots), `.claude/skills/mm-decision-maker/SKILL.md` (tally + final) |
| `export` | `.claude/skills/mm-pdf-exporter/SKILL.md` |
| `user_review` | `.claude/skills/mm-orchestrator/SKILL.md` |
| `reflect` | `.claude/skills/mm-memory-writer/SKILL.md` plus `eval/` graders |

## Skill Inventory

- `mm-catalyst-analyst`: catalyst timing and event-driven thesis.
- `mm-company-analyst`: company fundamentals memo and debate.
- `mm-company-desk`: company news, filings, catalyst calendar, fundamentals.
- `mm-company-resolver`: ticker, company profile, peers, market context.
- `mm-decision-maker`: decision-panel chair — per-round tally and final BUY/HOLD/SELL decision.
- `mm-decision-panelist`: casts one analyst role's ballot (vote + conviction + hedge overlay) per panel round.
- `mm-discussion-panelist`: files one analyst role's structured view (stance + conviction + challenges) per discussion-panel round.
- `mm-discussion-moderator`: discussion-panel chair — per-round tally and synthesis into thesis map + debate summary.
- `mm-init`: interactive workspace initialization.
- `mm-market-analyst`: macro and market environment memo.
- `mm-market-desk`: macro headlines, index data, macro assets.
- `mm-memory-writer`: episodic, semantic, and procedural memories.
- `mm-orchestrator`: full autonomous pipeline protocol.
- `mm-pdf-exporter`: report PDF export with committed templates.
- `mm-progress-monitor`: status watcher logic.
- `mm-quant-analyst`: technical indicators and relative strength.
- `mm-report-reviewer`: report scoring and revision brief.
- `mm-report-writer`: daily/weekly report drafting.
- `mm-risk-analyst`: risk memo and counterarguments.
- `mm-sector-desk`: sector news, peers, industry context.
- `mm-chips-analyst`: chip structure & game analysis (筹码博弈) — volume/flow directional evidence.
- `mm-valuation-analyst`: valuation memo.
- `mm-valuation-engine`: formula-first DCF/comps engine.
- `mm-web-research`: web/NASDAQ news collection with provenance.

