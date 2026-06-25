#!/usr/bin/env python3
"""Extract session analytics from Claude Code JSONL transcripts.

Duration notions (per session, toggleable in the dashboard):
  active : Σ active inter-event time, gaps capped at 120s (idle excluded).
  turn   : Σ turn_duration durationMs (Claude Code's own per-turn wall time).
  span   : calendar span end-start (includes all idle).

Granular filterable buckets per session:
  hd  : {"<dow>_<hour>": seconds}   cal : {"YYYY-MM-DD": seconds}

Content stats per session (own file + attributed subagent/workflow files):
  toolCounts {tool:n} · extCounts {ext:n} · bashCounts {verb:n}
  trans {"A>B":n} among TOP_TOOLS · nThinking · nToolErr · nToolResults
  token split: own tokensIn/Out/cacheRead/cacheCreate + subagent equivalents
"""
import json, os, re, glob, sys
from datetime import datetime
from collections import defaultdict

ROOT = os.path.expanduser("~/.claude/projects")
OUT = "/Users/user/projects/session-dashboard/data.js"
IDLE_CAP = 120.0

TS_RE = re.compile(rb'"timestamp":"([^"]+)"')
TOP_TOOLS = ["Bash","Read","Write","Edit","WebSearch","WebFetch",
             "ToolSearch","StructuredOutput","Agent","Workflow","TaskUpdate"]
TOPSET = set(TOP_TOOLS)

def parse_ts(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def short_project(dirname):
    name = dirname
    for pre in ("-Users-user-projects-github-com-dmitrykrivaltsevich-",
                "-Users-user-projects-github-com-",
                "-Users-user-projects--playground-",
                "-Users-user-projects-", "-Users-user-"):
        if name.startswith(pre):
            name = name[len(pre):]; break
    return name or dirname

# ---- discover files ----
all_jsonl = glob.glob(os.path.join(ROOT, "*", "**", "*.jsonl"), recursive=True)
top_sessions, file_parent = [], {}   # file_parent[path] = full session id it belongs to
for p in all_jsonl:
    parts = os.path.relpath(p, ROOT).split(os.sep)
    if "subagents" in parts or "workflows" in parts:
        if len(parts) >= 2: file_parent[p] = parts[1]
        continue
    if len(parts) == 2 and parts[-1].endswith(".jsonl"):
        sid = parts[-1][:-6]
        top_sessions.append((parts[0], p, sid))
        file_parent[p] = sid
valid_parents = {sid for _, _, sid in top_sessions}
print(f"top-level sessions: {len(top_sessions)}  total files: {len(all_jsonl)}", file=sys.stderr)

# ---- content-stats pass over ALL files, attributed to parent session ----
def blank():
    return {"tool":defaultdict(int),"ext":defaultdict(int),"bash":defaultdict(int),
            "trans":defaultdict(int),"think":0,"terr":0,"tres":0,
            "subIn":0,"subOut":0,"subCR":0,"subCC":0,"subFiles":0}
stats = defaultdict(blank)

def ext_of(fp):
    m = re.search(r'\.([A-Za-z0-9]{1,8})$', fp or "")
    return m.group(1).lower() if m else None

for p, parent in file_parent.items():
    if parent not in valid_parents: continue
    is_sub = parent != file_parent.get(p) or False
    is_sub = (os.path.relpath(p, ROOT).split(os.sep)[-1][:-6] != parent)
    st = stats[parent]
    if is_sub: st["subFiles"] += 1
    last_tool = None
    try:
        for raw in open(p, "rb"):
            if len(raw) > 1_500_000: continue
            if (b'"usage"' not in raw and b'tool_result' not in raw
                    and b'thinking' not in raw): continue
            try: d = json.loads(raw)
            except Exception: continue
            t = d.get("type")
            if t == "assistant":
                msg = d.get("message") or {}
                if is_sub:
                    u = msg.get("usage") or {}
                    st["subIn"]  += u.get("input_tokens",0) or 0
                    st["subOut"] += u.get("output_tokens",0) or 0
                    st["subCR"]  += u.get("cache_read_input_tokens",0) or 0
                    st["subCC"]  += u.get("cache_creation_input_tokens",0) or 0
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict): continue
                        bt = b.get("type")
                        if bt == "thinking":
                            st["think"] += 1
                        elif bt == "tool_use":
                            nm = b.get("name","?")
                            st["tool"][nm] += 1
                            if last_tool and nm in TOPSET and last_tool in TOPSET and nm != last_tool:
                                st["trans"][last_tool+">"+nm] += 1
                            last_tool = nm
                            inp = b.get("input") or {}
                            if nm == "Bash":
                                cmd = (inp.get("command") or "").strip()
                                first = re.split(r'\s+', cmd)[0].split('/')[-1] if cmd else ""
                                first = re.sub(r'[^A-Za-z0-9_.-]', '', first)
                                if first and not first[0].isdigit():
                                    st["bash"][first] += 1
                            elif nm in ("Read","Write","Edit","NotebookEdit"):
                                e = ext_of(inp.get("file_path") or inp.get("notebook_path") or "")
                                if e: st["ext"][e] += 1
            elif t == "user":
                msg = d.get("message") or {}
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            st["tres"] += 1
                            if b.get("is_error"): st["terr"] += 1
    except Exception as e:
        print("stats err", p, e, file=sys.stderr)

def topn(dd, n):
    return dict(sorted(dd.items(), key=lambda kv: -kv[1])[:n])

# ---- per-session core metrics from top-level files ----
sessions = []
for project_dir, path, sid in top_sessions:
    events, turns = [], []
    n_assist = n_user = n_tool = 0
    tin = tout = tcr = tcc = 0
    models, branch, version, cwd = {}, None, None, None
    try:
        for raw in open(path, "rb"):
            if not raw.strip(): continue
            m = TS_RE.search(raw)
            if m:
                dt = parse_ts(m.group(1).decode())
                if dt: events.append(dt.astimezone())
            if len(raw) > 1_500_000: continue
            try: d = json.loads(raw)
            except Exception: continue
            t = d.get("type")
            if d.get("subtype") == "turn_duration":
                v = d.get("durationMs")
                if isinstance(v, (int, float)): turns.append(v)
                continue
            if t == "assistant":
                msg = d.get("message") or {}
                if cwd is None: cwd = d.get("cwd")
                if d.get("gitBranch"): branch = d.get("gitBranch")
                if d.get("version"): version = d.get("version")
                mdl = msg.get("model")
                if mdl and mdl != "<synthetic>": models[mdl] = models.get(mdl,0)+1
                u = msg.get("usage") or {}
                tin += u.get("input_tokens",0) or 0
                tout += u.get("output_tokens",0) or 0
                tcr += u.get("cache_read_input_tokens",0) or 0
                tcc += u.get("cache_creation_input_tokens",0) or 0
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use": n_tool += 1
                n_assist += 1
            elif t == "user":
                n_user += 1
    except Exception as e:
        print("session err", path, e, file=sys.stderr); continue
    if not events: continue
    events.sort()
    start, end = events[0], events[-1]
    span_ms = (end - start).total_seconds() * 1000
    active_s = 0.0; hd = {}; cal = {}
    for a, b in zip(events, events[1:]):
        g = (b - a).total_seconds()
        if g <= 0: continue
        g = min(g, IDLE_CAP); active_s += g
        k = f"{a.weekday()}_{a.hour}"; hd[k] = hd.get(k,0)+g
        ds = a.strftime("%Y-%m-%d"); cal[ds] = cal.get(ds,0)+g

    st = stats.get(sid, blank())
    primary = max(models, key=models.get) if models else None
    sessions.append({
        "id": sid[:8], "project": short_project(project_dir),
        "branch": branch, "version": version,
        "start": start.isoformat(), "date": start.strftime("%Y-%m-%d"),
        "hour": start.hour, "dow": start.weekday(),
        "active": round(active_s*1000),
        "turn": round(sum(turns)) if turns else None,
        "span": round(span_ms), "estimated": not bool(turns),
        "turns": [round(x) for x in turns], "nTurns": len(turns),
        "nAssistant": n_assist, "nUser": n_user, "nToolUse": n_tool,
        "tokensIn": tin, "tokensOut": tout, "cacheRead": tcr, "cacheCreate": tcc,
        "models": models, "primaryModel": primary,
        "subTokensIn": st["subIn"], "subTokensOut": st["subOut"],
        "subCacheRead": st["subCR"], "subCacheCreate": st["subCC"], "nSubagents": st["subFiles"],
        # content stats
        "toolCounts": dict(st["tool"]),
        "extCounts": topn(st["ext"], 24),
        "bashCounts": topn(st["bash"], 18),
        "trans": dict(st["trans"]),
        "nThinking": st["think"], "nToolErr": st["terr"], "nToolResults": st["tres"],
        "hd": {k: round(v) for k,v in hd.items() if v >= 1},
        "cal": {k: round(v) for k,v in cal.items() if v >= 1},
    })

sessions.sort(key=lambda s: s["start"])
out = {"generatedAt": datetime.now().astimezone().isoformat(),
       "idleCapSec": IDLE_CAP, "topTools": TOP_TOOLS,
       "nSessions": len(sessions), "sessions": sessions}
with open(OUT, "w") as fh:
    fh.write("const DATA = "); json.dump(out, fh, separators=(",",":")); fh.write(";\n")

tot = sum(s["active"] for s in sessions)
tools = sum(sum(s["toolCounts"].values()) for s in sessions)
print(f"sessions={len(sessions)} activeH={tot/3600000:.1f} toolCalls={tools} "
      f"thinking={sum(s['nThinking'] for s in sessions)} bytes={os.path.getsize(OUT)}", file=sys.stderr)
