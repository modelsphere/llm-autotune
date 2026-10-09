# Prompting the report agent

How to start a Claude Code session that writes a performance report with the
`perf-report` skill (`agent_skills/perf-report/`).

## What goes where

| Lives in | What |
|---|---|
| **The prompt** (below) | This report's inputs (campaign, baseline, attempts, names, title) and the operator guardrails (stay in `work/`, no `--force`, don't ask questions) |
| `SKILL.md` | The workflow: `compare` → write → `check` → `save`, and where each fact sits in `comparison.json` |
| `TEMPLATE.md` | The report's structure, blocks, style rules, scenario naming and bilingual glossary |
| The platform's renderer (`frontend/src/report/`) | How every chart, table and command looks, and every sentence that must read the same in all reports: numbered captions, metric definitions, table notes |

Keep the prompt to the first row. Anything about how the report reads belongs in
`TEMPLATE.md`, and anything about how it looks belongs in the renderer.

- **Renderer changes need no new prompt**, and no agent run. The markdown only names
  blocks (`chart type: sweep`), so a new chart layout reaches saved reports on
  the next page load.
- **Template or skill changes need no new prompt either.** The prompt says to follow
  `SKILL.md` and `TEMPLATE.md`, so the next session picks them up. Reports
  already saved keep their prose until someone runs the agent again.

## Before starting Claude Code

Run once per shell, in the repo checkout or in any folder that holds `agent_skills/`:

```bash
export AUTOTUNE_URL=https://autotune.example.com      # the platform API
export AUTOTUNE_UI_URL=https://autotune.example.com   # the web UI, for report links
export AUTOTUNE_API_KEY=atk_...                       # a key of a non-admin user
claude
```

## The prompt

Fill in the fields at the top. Everything below them stays as it is.

- **Names** is optional. Delete the line and each language gets its glossary
  names (Baseline / Optimized, 基线 / 优化配置). Names you pass show as written
  in both languages.
- **Scenarios** is optional too: what to call each workload in both languages,
  in the order the campaign measures them. Leave it out and the agent names them
  from their input/output shape (`TEMPLATE.md` → "Naming the scenarios").
- **Baseline** and **Attempts** are run ids; attempts go in the order the
  report should tell them, comma-separated.

```
Campaign: [42]
Baseline: [41]
Attempts: [47, 52]
Names: [Prod, fp8 + 32k prefill]
Scenarios: [Agent long-context / Agent 长上下文场景, Document QA / 长文档问答场景]
Title (English): [Optimizing Qwen3-32B on H100]
Title (Chinese): [在 H100 上优化 Qwen3-32B]

Write a performance report for the campaign above on our LLM AutoTune platform and save it there, in English and in Chinese.

Setup:
- Read agent_skills/perf-report/SKILL.md and follow it exactly, and agent_skills/perf-report/TEMPLATE.md for the report itself. All platform access goes through agent_skills/perf-report/scripts/autotune_report.py; do not call the API any other way and do not query the database.
- The platform URL and API key are in AUTOTUNE_URL and AUTOTUNE_API_KEY. If `python3 agent_skills/perf-report/scripts/autotune_report.py campaigns` fails, stop and show me the error.
- Work in ./work/. Do not modify anything outside it.

The report:
- Baseline and Attempts are runs of the campaign, attempts in the order given. Every other run of the campaign is out of scope.
- If Names is given, pass it as --labels on both saves (baseline first). If not, pass no --labels.
- If Scenarios is given, use those names in work/scenarios.en.json and work/scenarios.zh.json and write the descriptions yourself; if not, name the scenarios as the template says.
- Use the two titles above for the English and the Chinese report.
- If the platform says the runs are not comparable, do not use --force. Stop and show me the reasons.

Finish when both reports pass `check` and are saved (English first, Chinese as its translation). Reply with both report links, the source zip link the script printed, and the conclusion's headline sentence. List any choice you made that the template did not decide for you, and anything in the data that looked wrong. Keep those notes in your reply, not in the report. Do not ask me questions along the way.
```
