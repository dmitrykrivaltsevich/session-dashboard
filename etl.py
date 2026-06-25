#!/usr/bin/env python3
"""Extract session analytics from Claude Code JSONL transcripts.

Duration: active (idle-capped) / turn (turn_duration) / span (calendar).
Content: tools, file types, shell verbs, tool transitions, thinking, errors.
Work:    skills, web domains/queries, file hotspots, LOC churn.
Cost:    per-model token classes (input/output/cache-read/cache-write-5m/-1h)
         so dollar cost can be computed at current per-model + per-cache rates.
"""
import json, os, re, glob, sys
from datetime import datetime
from collections import defaultdict

ROOT = os.path.expanduser("~/.claude/projects")
OUT = "/Users/user/projects/session-dashboard/data.js"
IDLE_CAP = 120.0

# Current list pricing ($/1M tokens) from the claude-api reference, plus cache
# multipliers (cache read = 0.1x input, 5-min write = 1.25x, 1-hour write = 2x).
PRICING = {
    "models": {
        "claude-fable-5":   [10.0, 50.0],
        "claude-opus-4-8":  [5.0, 25.0],
        "claude-opus-4-7":  [5.0, 25.0],
        "claude-opus-4-6":  [5.0, 25.0],
        "claude-sonnet-4-6":[3.0, 15.0],
        "claude-haiku-4-5": [1.0, 5.0],
    },
    "cacheRead": 0.1, "cacheWrite5m": 1.25, "cacheWrite1h": 2.0,
    "note": "public list prices, $/1M tokens; cache read 0.1x input, 5-min write 1.25x, 1-hour write 2x. Local/non-Anthropic models priced at $0.",
}

TS_RE = re.compile(rb'"timestamp":"([^"]+)"')
TOP_TOOLS = ["Bash","Read","Write","Edit","WebSearch","WebFetch",
             "ToolSearch","StructuredOutput","Agent","Workflow","TaskUpdate"]
TOPSET = set(TOP_TOOLS)
EXT_RE = re.compile(r'\.([A-Za-z0-9]{1,8})$')
URL_RE = re.compile(r'https?://([^/]+)')
WORD_RE = re.compile(r'[a-z]{4,}')
STOP = {"with","that","this","from","what","https","html","your","have","page",
        "about","into","does","when","they","will","them","then","there","which",
        "their","would","could","should","github"}

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

def norm_path(fp):
    fp = re.sub(r'^/Users/[^/]+/', '~/', fp or "")
    parts = fp.split('/')
    return '/'.join(parts[-4:]) if len(parts) > 4 else fp

all_jsonl = glob.glob(os.path.join(ROOT, "*", "**", "*.jsonl"), recursive=True)
top_sessions, file_parent = [], {}
for p in all_jsonl:
    parts = os.path.relpath(p, ROOT).split(os.sep)
    if "subagents" in parts or "workflows" in parts:
        if len(parts) >= 2: file_parent[p] = parts[1]
        continue
    if len(parts) == 2 and parts[-1].endswith(".jsonl"):
        sid = parts[-1][:-6]
        top_sessions.append((parts[0], p, sid)); file_parent[p] = sid
valid_parents = {sid for _, _, sid in top_sessions}
print(f"top-level sessions: {len(top_sessions)}  total files: {len(all_jsonl)}", file=sys.stderr)

# ---- content + work + cost pass over ALL files, attributed to parent ----
def mtok():
    return defaultdict(lambda: {"in":0,"out":0,"cr":0,"cc5":0,"cc1":0})
def blank():
    return {"tool":defaultdict(int),"ext":defaultdict(int),"bash":defaultdict(int),
            "trans":defaultdict(int),"think":0,"terr":0,"tres":0,
            "skill":defaultdict(int),"domain":defaultdict(int),"kw":defaultdict(int),
            "file":defaultdict(int),"locAdd":0,"locDel":0,"writes":0,"writeLines":0,
            "subIn":0,"subOut":0,"subCR":0,"subCC":0,"subFiles":0,"modelTok":mtok()}
stats = defaultdict(blank)

for p, parent in file_parent.items():
    if parent not in valid_parents: continue
    is_sub = (os.path.relpath(p, ROOT).split(os.sep)[-1][:-6] != parent)
    st = stats[parent]
    if is_sub: st["subFiles"] += 1
    last_tool = None
    try:
        for raw in open(p, "rb"):
            if len(raw) > 1_500_000: continue
            if (b'"usage"' not in raw and b'tool_result' not in raw
                    and b'thinking' not in raw and b'attributionSkill' not in raw): continue
            try: d = json.loads(raw)
            except Exception: continue
            t = d.get("type")
            if t == "assistant":
                msg = d.get("message") or {}
                if d.get("attributionSkill"): st["skill"][d["attributionSkill"]] += 1
                u = msg.get("usage") or {}
                mdl = msg.get("model")
                if mdl and mdl != "<synthetic>":
                    mt = st["modelTok"][mdl]
                    mt["in"]  += u.get("input_tokens",0) or 0
                    mt["out"] += u.get("output_tokens",0) or 0
                    mt["cr"]  += u.get("cache_read_input_tokens",0) or 0
                    cc = u.get("cache_creation") or {}
                    c5 = cc.get("ephemeral_5m_input_tokens"); c1 = cc.get("ephemeral_1h_input_tokens")
                    if c5 is None and c1 is None:  # no split available -> treat as 5-min
                        mt["cc5"] += u.get("cache_creation_input_tokens",0) or 0
                    else:
                        mt["cc5"] += c5 or 0; mt["cc1"] += c1 or 0
                if is_sub:
                    st["subIn"]  += u.get("input_tokens",0) or 0
                    st["subOut"] += u.get("output_tokens",0) or 0
                    st["subCR"]  += u.get("cache_read_input_tokens",0) or 0
                    st["subCC"]  += u.get("cache_creation_input_tokens",0) or 0
                cont = msg.get("content")
                if isinstance(cont, list):
                    for b in cont:
                        if not isinstance(b, dict): continue
                        bt = b.get("type")
                        if bt == "thinking":
                            st["think"] += 1
                        elif bt == "tool_use":
                            nm = b.get("name","?"); st["tool"][nm] += 1
                            if last_tool and nm in TOPSET and last_tool in TOPSET and nm != last_tool:
                                st["trans"][last_tool+">"+nm] += 1
                            last_tool = nm
                            inp = b.get("input") or {}
                            if nm == "Bash":
                                cmd = (inp.get("command") or "").strip()
                                first = re.split(r'\s+', cmd)[0].split('/')[-1] if cmd else ""
                                first = re.sub(r'[^A-Za-z0-9_.-]', '', first)
                                if first and not first[0].isdigit(): st["bash"][first] += 1
                            elif nm == "WebFetch":
                                m = URL_RE.search(inp.get("url","") or "")
                                if m: st["domain"][m.group(1).replace("www.","")] += 1
                            elif nm == "WebSearch":
                                for w in WORD_RE.findall((inp.get("query") or "").lower()):
                                    if w not in STOP: st["kw"][w] += 1
                            elif nm in ("Read","Write","Edit","NotebookEdit"):
                                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                                m = EXT_RE.search(fp)
                                if m: st["ext"][m.group(1).lower()] += 1
                                if fp: st["file"][norm_path(fp)] += 1
                                if nm == "Edit":
                                    o = inp.get("old_string") or ""; n = inp.get("new_string") or ""
                                    ol = o.count("\n")+1 if o else 0; nl = n.count("\n")+1 if n else 0
                                    if nl > ol: st["locAdd"] += nl-ol
                                    else: st["locDel"] += ol-nl
                                elif nm == "Write":
                                    st["writes"] += 1
                                    st["writeLines"] += (inp.get("content") or "").count("\n")+1
            elif t == "user":
                cont = (d.get("message") or {}).get("content")
                if isinstance(cont, list):
                    for b in cont:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            st["tres"] += 1
                            if b.get("is_error"): st["terr"] += 1
    except Exception as e:
        print("stats err", p, e, file=sys.stderr)

def topn(dd, n): return dict(sorted(dd.items(), key=lambda kv: -kv[1])[:n])

# ---- per-session core metrics from top-level files ----
sessions = []
for project_dir, path, sid in top_sessions:
    events, turns = [], []
    n_assist = n_user = n_tool = 0
    tin = tout = tcr = tcc = 0
    models, branch, version = {}, None, None
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
                if d.get("gitBranch"): branch = d.get("gitBranch")
                if d.get("version"): version = d.get("version")
                mdl = msg.get("model")
                if mdl and mdl != "<synthetic>": models[mdl] = models.get(mdl,0)+1
                u = msg.get("usage") or {}
                tin += u.get("input_tokens",0) or 0
                tout += u.get("output_tokens",0) or 0
                tcr += u.get("cache_read_input_tokens",0) or 0
                tcc += u.get("cache_creation_input_tokens",0) or 0
                cont = msg.get("content")
                if isinstance(cont, list):
                    for b in cont:
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
    mtok_out = {m: v for m, v in st["modelTok"].items()}
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
        "toolCounts": dict(st["tool"]), "extCounts": topn(st["ext"], 24),
        "bashCounts": topn(st["bash"], 18), "trans": dict(st["trans"]),
        "nThinking": st["think"], "nToolErr": st["terr"], "nToolResults": st["tres"],
        # work
        "skills": topn(st["skill"], 16), "domains": topn(st["domain"], 14),
        "searchKw": topn(st["kw"], 24), "files": topn(st["file"], 12),
        "locAdd": st["locAdd"], "locDel": st["locDel"],
        "writes": st["writes"], "writeLines": st["writeLines"],
        # cost
        "modelTok": mtok_out,
        "hd": {k: round(v) for k,v in hd.items() if v >= 1},
        "cal": {k: round(v) for k,v in cal.items() if v >= 1},
    })

sessions.sort(key=lambda s: s["start"])
out = {"generatedAt": datetime.now().astimezone().isoformat(),
       "idleCapSec": IDLE_CAP, "topTools": TOP_TOOLS, "pricing": PRICING,
       "nSessions": len(sessions), "sessions": sessions}
with open(OUT, "w") as fh:
    fh.write("const DATA = "); json.dump(out, fh, separators=(",",":")); fh.write(";\n")

# cost summary
PM = PRICING["models"]; cr_m, w5_m, w1_m = PRICING["cacheRead"], PRICING["cacheWrite5m"], PRICING["cacheWrite1h"]
tot = 0.0
for s in sessions:
    for m, tk in s["modelTok"].items():
        p = PM.get(m)
        if not p: continue
        tot += (tk["in"]*p[0] + tk["out"]*p[1] + tk["cr"]*p[0]*cr_m
                + tk["cc5"]*p[0]*w5_m + tk["cc1"]*p[0]*w1_m) / 1e6
print(f"sessions={len(sessions)} activeH={sum(s['active'] for s in sessions)/3.6e6:.1f} "
      f"estCost=${tot:,.0f} loc=+{sum(s['locAdd'] for s in sessions)}/-{sum(s['locDel'] for s in sessions)} "
      f"writes={sum(s['writes'] for s in sessions)} bytes={os.path.getsize(OUT)}", file=sys.stderr)
