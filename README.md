# Claude Code · Session Analytics Dashboard

Interactive dashboard exploring **how long your Claude Code sessions actually take**
(API wall time), built from your local session transcripts in `~/.claude/projects/`.

## Open it

```bash
open dashboard.html         # macOS default browser
```

Everything is local and self-contained — `dashboard.html` loads `data.js` (your
extracted data) and `echarts.min.js` (the charting library) from this folder. No
network needed.

## Regenerate the data

```bash
python3 etl.py             # re-scans ~/.claude/projects -> data.js
```

## The three duration metrics (toggle in the UI)

| Metric | Definition |
|---|---|
| **Active time** *(default)* | Σ of gaps between successive session events, with idle gaps (> 120 s) removed. A uniform, idle-free estimate available for **every** session. Calibrated to ≈1.08× of Claude Code's own turn timing. |
| **Turn time** | Σ of Claude Code's recorded `turn_duration` values (true per-turn wall clock, incl. in-turn waits). Available for the sessions that recorded it; falls back to Active time otherwise (marked `est`). |
| **Calendar span** | First → last event timestamp, including all idle (e.g. sessions resumed across days). |

The **heatmap** and **calendar** always use idle-free Active time (they answer
"*when* was I engaged"); the metric toggle drives the per-session views (KPIs,
distribution, project breakdown, scatter, table).

## Views

**Time & duration**
- **Activity over time** — daily totals + 7-day moving average
- **When I work** — hour-of-day × day-of-week heatmap
- **Session length distribution** — histogram of session durations
- **Calendar of active time** — GitHub-style contribution map
- **Time by project** — where the hours go
- **Duration vs output tokens** — scatter (bubble = tool calls, colour = project)
- **Turn length distribution** — histogram of individual `turn_duration` values
- **Model mix** — share of active time per model

**Anatomy of the work** (what Claude did inside the sessions, incl. subagents)
- **Tool usage** — nested treemap of all tool calls by category (Shell / Files / Web / Agents / Browser)
- **Tool workflow** — network graph of consecutive tool-to-tool transitions (Bash↔Read, WebSearch↔WebFetch…)
- **File-type footprint** — treemap of file extensions read/written/edited
- **Shell commands** — frequency of the first verb of every `Bash` call
- **Throughput** — output tokens produced per active hour, by project
- **Token economics** — input vs cache-write vs cache-read vs output (the prompt cache served **94.6%** of input tokens)

- **Sessions table** — sortable, reflects all filters

## Filters

Metric · date range (All / 30d / 14d / 7d) · model · project chips
(click one to isolate, click again to restore all). Everything updates live.

## Scope

Analyses the **70 top-level session files** (67 with usable timestamps),
attributing the **1,511 subagent / workflow** transcripts' tokens to their parent
sessions.
