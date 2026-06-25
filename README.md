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
- **Token economics** — input vs cache-write vs cache-read vs output (the prompt cache serves ~95% of input tokens)

**What I work on, make & spend**
- **Skills & power-features** — invocations of deep-research, the speckit SDD suite, kb, drive (how you actually drive Claude)
- **Research footprint** — top web domains fetched
- **Search themes** — word-cloud treemap of every `WebSearch` query term
- **Output ledger** — lines added/removed per project (Edit churn)
- **Codebase hotspots** — treemap of the most-touched files
- **Estimated cost** — per-project spend, plus "$ saved by the cache" (see Cost below)

- **Sessions table** — sortable, reflects all filters

## Cost estimation

Cost is an **estimate at current public list prices** (you may pay differently on a
subscription/Max plan). It is computed **per model** and **per token class**, because
mispricing the cache swings the total several-fold:

| Token class | Rate (× model input price) |
|---|---|
| Input | 1× |
| Output | model output price |
| Cache **read** | 0.1× |
| Cache **write**, 5-min TTL | 1.25× |
| Cache **write**, 1-hour TTL | 2× |

Per-model rates ($/1M tokens) live in `data.js` under `pricing` (Opus 4.8 $5/$25,
Opus 4.7 $5/$25, Fable 5 $10/$50, Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15). Local /
non-Anthropic models (e.g. `google/gemma-*`) are priced at $0. "Saved by cache" =
what the cache-read tokens *would* have cost at full input price minus what they
actually cost at 0.1×.

## Filters

Metric · date range (All / 30d / 14d / 7d) · model · project chips
(click one to isolate, click again to restore all). Everything updates live.

## Scope

Analyses the **70 top-level session files** (67 with usable timestamps),
attributing the **1,511 subagent / workflow** transcripts' tokens to their parent
sessions.
