# agent_skills

Claude Code skills that work against the platform's [agent API](../docs/api/agent-api.md).
A skill is a folder: `SKILL.md` (what to do) plus `scripts/` (how to talk to
the platform). It carries no platform code — everything it needs is behind
`/api/agent/v1/` — so it runs on any machine that can reach the platform.

| Skill | What it does |
| --- | --- |
| [`perf-report/`](perf-report/) | Writes a baseline-vs-attempts performance report for one campaign and saves it on the platform (`/reports`). |

## Install on any machine

```bash
# once: copy the skill where Claude Code looks for skills
mkdir -p ~/.claude/skills
cp -r agent_skills/perf-report ~/.claude/skills/perf-report

# every session: where the platform is and who you are
export AUTOTUNE_URL=https://autotune.example.com
export AUTOTUNE_API_KEY=at_...    # minted on the platform's API keys page, by the `reporter` user

claude
> /perf-report  write the report for campaign 42: run 101 is the baseline, runs 103 and 104 are the attempts
```

Inside this repository the skill is also reachable as `.claude/skills/perf-report`
(a symlink), so a Claude Code session started here has it without copying.

A ready-to-paste prompt for a fresh session is in [`docs/perf-report-prompt.md`](../docs/perf-report-prompt.md).
The report's structure, wording and glossary live in `perf-report/TEMPLATE.md`;
every report is written in English and in Chinese. Charts, tables and serving
commands are blocks the platform draws from the report's frozen data — the agent
writes prose and places blocks. Each saved report has a self-contained HTML
export (`GET /api/agent/v1/reports/{id}/export.html`, or the Export HTML button)
carrying both languages, ready for a docs or blog site, and a source zip
(`GET /api/agent/v1/reports/{id}/bundle.zip`, or the Download source button) with
the markdown, the data and the renderer. Set `AUTOTUNE_UI_URL`
(e.g. `https://autotune.example.com`) to get full report links from `save`.
