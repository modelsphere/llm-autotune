# Prompting the report agent

How to start a Claude Code session that writes a performance report with the
`perf-report` skill (`agent_skills/perf-report/`).

## What goes where

| Lives in | What |
|---|---|
| **The prompt** (below) | This report's inputs (campaign, baseline, attempts, names, title) and the operator guardrails (stay in `work/`, no `--force`, don't ask questions) |
| `SKILL.md` | The workflow: `compare` → write → `check` → `save`, and where each fact sits in `comparison.json` |
| `TEMPLATE.md` | The report's structure, blocks, style rules and bilingual glossary |
| The platform's renderer (`frontend/src/report/`) | How every chart, table and command looks |

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
export AUTOTUNE_API_KEY=atk_...                   # the reporter key
claude
```

## The prompt

Fill in the fields at the top. Everything below them stays as it is.

- **Names** is optional. Delete the line and each language gets its glossary
  names (Baseline / Optimized, 基线 / 优化配置). Names you pass show as written
  in both languages.
- **Attempts** go in the order the report should tell them, comma-separated.

```
Campaign: [42]
Baseline: [s42]
Attempts: [s43]
Names: [Prod, fp8 + 32k prefill]
Title (English): [Optimizing Qwen3.6-35B-A3B on H800]
Title (Chinese): [在 H800 上优化 Qwen3.6-35B-A3B]

Write a performance report for the campaign above on our LLM AutoTune platform and save it there, in English and in Chinese.

Setup:
- Read agent_skills/perf-report/SKILL.md and follow it exactly, and agent_skills/perf-report/TEMPLATE.md for the report itself. All platform access goes through agent_skills/perf-report/scripts/autotune_report.py; do not call the API any other way and do not query the database.
- The platform URL and API key are in AUTOTUNE_URL and AUTOTUNE_API_KEY. If `python3 agent_skills/perf-report/scripts/autotune_report.py tracks` fails, stop and show me the error.
- Work in ./work/. Do not modify anything outside it.

The report:
- Baseline and Attempts are runs of the campaign, attempts in the order given. Every other submission on the track is out of scope.
- If Names is given, pass it as --labels on both saves (baseline first). If not, pass no --labels.
- Use the two titles above for the English and the Chinese report.
- If the platform says the runs are not comparable, do not use --force. Stop and show me the reasons.

Finish when both reports pass `check` and are saved (English first, Chinese as its translation). Reply with both report links, the source zip link the script printed, and the conclusion's headline sentence. List any choice you made that the template did not decide for you, and anything in the data that looked wrong. Keep those notes in your reply, not in the report. Do not ask me questions along the way.
```

## Changes from the 2026-09-16 prompt

That prompt was written for the first version of the skill. Don't reuse it.

| The old prompt said | Now |
|---|---|
| Install matplotlib; use charts from `work/charts/` | No images: charts are `chart` blocks the platform draws |
| Paste tables from `work/tables.md` | Tables and serving commands are blocks from `work/blocks.md`, checked on save |
| Pick a template, or the default outline in SKILL.md | `TEMPLATE.md` is the only outline |
| `Language: [English]` | Always both languages; the Chinese report is saved with `--translation-of` |
| Write "not recorded" for missing facts | Leave the fact out; the template's rules decide what appears |
| For a failed attempt, say why it failed | Report what it changed and the failure message, without explaining why (the template's "report, don't judge" rule) |
| Reply with the gain "and whether it still passes all redlines" | Reply with the headline sentence; gate status is not in the report |
| Name no config labels | Names go in `--labels` |
