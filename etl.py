#!/usr/bin/env python3
"""Extract session analytics from Claude Code JSONL transcripts.

Reads Claude Code's local session transcripts (default ~/.claude/projects) and
writes a compact ``data.js`` consumed by ``dashboard.html``.

Usage:
    python3 etl.py                       # scan ~/.claude/projects -> ./data.js
    python3 etl.py --root /path/to/projects --out /path/to/data.js
    CLAUDE_PROJECTS_DIR=... python3 etl.py

No third-party dependencies (Python 3.8+ standard library only).

Duration: active (idle-capped) / turn (turn_duration) / span (calendar).
Content : tools, file types, shell verbs, tool transitions, thinking, errors.
Work    : skills, web domains/queries, file hotspots, LOC churn.
Cost    : per-model token classes (input/output/cache-read/cache-write-5m/-1h)
          so dollar cost can be computed at current per-model + per-cache rates.
"""
import argparse, json, os, re, glob, sys, subprocess
from datetime import datetime
from collections import defaultdict

IDLE_CAP = 120.0
HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Current list pricing ($/1M tokens) plus cache multipliers (cache read = 0.1x
# input, 5-min write = 1.25x, 1-hour write = 2x). Update here when rates change.
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
BASH_CLEAN = re.compile(r'[^A-Za-z0-9_.-]')

def add_usage(dst, u):
    """Accumulate the 4 plain token fields of a usage object into dst (in/out/cr/cc)."""
    dst["in"]  += u.get("input_tokens",0) or 0
    dst["out"] += u.get("output_tokens",0) or 0
    dst["cr"]  += u.get("cache_read_input_tokens",0) or 0
    dst["cc"]  += u.get("cache_creation_input_tokens",0) or 0
STOP = {"with","that","this","from","what","https","html","your","have","page",
        "about","into","does","when","they","will","them","then","there","which",
        "their","would","could","should","github"}
# user prompts that are harness/agent boilerplate, not real "missions"
BOIL = re.compile(r'^\s*(Base directory for this skill|Run the "|You are \**Agent|'
                  r'Caveat:|<|\[Request interrupted|This session is being continued|'
                  r'Please continue|Your task is to)', re.I)
# Affirmations/approvals — NOT corrections even if they contain a trigger word
# (e.g. "yes remove it", "I want all of them"). Checked at message start.
AFFIRM = re.compile(r"^\s*(yes|yep|yeah|ya|ok|okay|sure|agreed?|do it|go ahead|"
                    r"please (do|run|proceed|go)|continue|proceed|sounds good|looks good|"
                    r"lgtm|great|perfect|nice work|merge it|approved?|i want all|all of)\b", re.I)
# General-purpose correction/redirect/dissatisfaction heuristic lexicon
# (negative-quality judgments, missed requirements, redirects, pushback; English
# + a little Russian). It is a fixed built-in matcher — NOT derived from any one
# user's data — so it works for anyone; tune it here for your own phrasing.
# The dashboard shows the actual phrases it flagged in *your* transcripts.
CORRECTION = re.compile(
    r"not (nice|good|right|mine|what i|correct)|(very )?bad (implementation|impl|comment|idea|design|naming|approach)|"
    r"\bi hate\b|\bugly\b|\bterrible\b|\bawful\b|\bnonsense\b|\bcrap\b|too (many|much)|"
    r"\bwrong\b|\bincorrect\b|does ?n'?t work|did ?n'?t work|\bbroke(n)?\b|messed up|\bmistake\b|"
    r"that'?s (not|wrong|bad)|forgot to (tell|mention|say|add)|"
    r"you (forgot|missed|removed|deleted|broke|must|should|did ?n'?t)|make sure|"
    r"\bdo not\b|don'?t (use|add|do that|argue|remove|change|forget)|instead of|"
    r"\brevert\b|\bundo\b|\brollback\b|\bredo\b|why (did|are|is|do(es)?) (you|it|we|this)|"
    r"should ?n'?t (we|you|it)|isn'?t there|but what if|still (see|seeing|not|broken|fail)|"
    r"\bне то\b|неправильн|\bговно\b|\bплохо\b|так нельзя|не так|че за|что за",
    re.I)

def git(args, cwd, timeout=30):
    try:
        r = subprocess.run(["git","-C",cwd]+args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def parse_ts(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

def fallback_project(dirname):
    """Derive a readable name from an encoded project dir when cwd is absent.
    Strips a generic /<home>/<user>/, projects/, and <forge>/<owner>/ prefix —
    no machine-specific names hardcoded."""
    s = re.sub(r'^-(Users|home|root)-[^-]+-', '', dirname)
    s = re.sub(r'^projects-+', '', s)
    s = re.sub(r'^(github-com|gitlab-com|bitbucket-org)-[^-]+-', '', s)
    return s.lstrip('-') or dirname

def project_name(cwd, dirname):
    """Prefer the basename of the session's recorded cwd; fall back to the dir."""
    if cwd:
        base = os.path.basename(cwd.rstrip('/'))
        if base: return base
    return fallback_project(dirname)

def norm_path(fp):
    """Home-relative, last 4 segments — keeps paths short and user-agnostic."""
    fp = fp or ""
    if fp.startswith(HOME): fp = "~" + fp[len(HOME):]
    parts = fp.split('/')
    return '/'.join(parts[-4:]) if len(parts) > 4 else fp

def discover(root):
    all_jsonl = glob.glob(os.path.join(root, "*", "**", "*.jsonl"), recursive=True)
    top_sessions, file_parent = [], {}
    for p in all_jsonl:
        parts = os.path.relpath(p, root).split(os.sep)
        if "subagents" in parts or "workflows" in parts:
            if len(parts) >= 2: file_parent[p] = parts[1]
            continue
        if len(parts) == 2 and parts[-1].endswith(".jsonl"):
            sid = parts[-1][:-6]
            top_sessions.append((parts[0], p, sid)); file_parent[p] = sid
    return all_jsonl, top_sessions, file_parent

def err_sig(content):
    """Normalize a tool error into a stable signature (strip paths/numbers/quotes)."""
    if isinstance(content, list):
        content = " ".join(b.get("text","") for b in content if isinstance(b, dict))
    s = (content or "").strip().split("\n")[0]
    s = re.sub(r'/[^\s]+', '<path>', s)
    s = re.sub(r'"[^"]*"|`[^`]*`', '"…"', s)
    s = re.sub(r'\b\d[\d.,]*\b', 'N', s)
    s = re.sub(r'\s+', ' ', s).strip()[:72]
    return s or "unknown error"

def mtok():
    return defaultdict(lambda: {"in":0,"out":0,"cr":0,"cc5":0,"cc1":0})
def blank():
    return {"tool":defaultdict(int),"ext":defaultdict(int),"bash":defaultdict(int),
            "trans":defaultdict(int),"think":0,"terr":0,"tres":0,
            "skill":defaultdict(int),"domain":defaultdict(int),"kw":defaultdict(int),
            "file":defaultdict(int),"locAdd":0,"locDel":0,"writes":0,"writeLines":0,
            "errsig":defaultdict(int),"subs":[],
            "subIn":0,"subOut":0,"subCR":0,"subCC":0,"subFiles":0,"modelTok":mtok()}

def content_stats(root, file_parent, valid_parents):
    stats = defaultdict(blank)
    for p, parent in file_parent.items():
        if parent not in valid_parents: continue
        is_sub = (os.path.relpath(p, root).split(os.sep)[-1][:-6] != parent)
        st = stats[parent]
        if is_sub: st["subFiles"] += 1
        last_tool = None
        sub = {"model":None,"in":0,"out":0,"cr":0,"cc":0,"nTools":0,"nWrites":0,"t0":None,"t1":None} if is_sub else None
        try:
            for raw in open(p, "rb"):
                if len(raw) > 1_500_000: continue
                if (b'"usage"' not in raw and b'tool_result' not in raw
                        and b'thinking' not in raw and b'attributionSkill' not in raw): continue
                try: d = json.loads(raw)
                except Exception: continue
                t = d.get("type")
                if sub is not None:
                    tsd = parse_ts(d.get("timestamp","")) if d.get("timestamp") else None
                    if tsd:
                        ms = int(tsd.timestamp()*1000)
                        if sub["t0"] is None or ms < sub["t0"]: sub["t0"] = ms
                        if sub["t1"] is None or ms > sub["t1"]: sub["t1"] = ms
                if t == "assistant":
                    msg = d.get("message") or {}
                    if d.get("attributionSkill"): st["skill"][d["attributionSkill"]] += 1
                    u = msg.get("usage") or {}
                    mdl = msg.get("model")
                    if sub is not None:
                        if mdl and mdl != "<synthetic>": sub["model"] = mdl
                        add_usage(sub, u)
                    if mdl and mdl != "<synthetic>":
                        mt = st["modelTok"][mdl]
                        mt["in"]  += u.get("input_tokens",0) or 0
                        mt["out"] += u.get("output_tokens",0) or 0
                        mt["cr"]  += u.get("cache_read_input_tokens",0) or 0
                        cc = u.get("cache_creation") or {}
                        c5 = cc.get("ephemeral_5m_input_tokens"); c1 = cc.get("ephemeral_1h_input_tokens")
                        if c5 is None and c1 is None:
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
                                if sub is not None:
                                    sub["nTools"] += 1
                                    if nm in ("Write","Edit","NotebookEdit"): sub["nWrites"] += 1
                                if last_tool and nm in TOPSET and last_tool in TOPSET and nm != last_tool:
                                    st["trans"][last_tool+">"+nm] += 1
                                last_tool = nm
                                inp = b.get("input") or {}
                                if nm == "Bash":
                                    cmd = (inp.get("command") or "").split()
                                    first = BASH_CLEAN.sub('', cmd[0].split('/')[-1]) if cmd else ""
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
                                if b.get("is_error"):
                                    st["terr"] += 1
                                    st["errsig"][err_sig(b.get("content"))] += 1
        except Exception as e:
            print("stats err", p, e, file=sys.stderr)
        if sub is not None and (sub["in"] or sub["out"] or sub["nTools"]):
            st["subs"].append(sub)
    return stats

def topn(dd, n): return dict(sorted(dd.items(), key=lambda kv: -kv[1])[:n])

def session_metrics(root, top_sessions, stats):
    sessions = []
    for project_dir, path, sid in top_sessions:
        events, turns, asks = [], [], []
        n_assist = n_user = n_tool = 0
        tin = tout = tcr = tcc = 0
        corr = interr = compact = 0; corr_ex = []
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
                stp = d.get("subtype")
                if stp == "turn_duration":
                    v = d.get("durationMs")
                    if isinstance(v, (int, float)): turns.append(v)
                    continue
                if stp and ("compact" in stp or stp == "away_summary"):
                    compact += 1; continue
                if t == "last-prompt":
                    pr = (d.get("lastPrompt") or "").strip()
                    if len(pr) >= 12 and not BOIL.match(pr):
                        pr = re.sub(r'\s+', ' ', pr)[:160]
                        if not asks or asks[-1] != pr: asks.append(pr)
                    continue
                if t == "assistant":
                    msg = d.get("message") or {}
                    if cwd is None and d.get("cwd"): cwd = d.get("cwd")
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
                    c = (d.get("message") or {}).get("content")
                    txt = c if isinstance(c, str) else (" ".join(
                        b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text") if isinstance(c, list) else "")
                    txt = txt.strip()
                    if txt and not txt.startswith("<"):
                        if "request interrupted" in txt.lower(): interr += 1
                        elif len(txt) < 170 and CORRECTION.search(txt) and not AFFIRM.match(txt):
                            corr += 1
                            if len(corr_ex) < 5: corr_ex.append(txt[:90])
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
            "id": sid[:8], "project": project_name(cwd, project_dir),
            "branch": branch, "version": version,
            "mission": asks[0] if asks else None, "asks": asks[:8],
            "_cwd": cwd, "repoRel": norm_path(cwd) if cwd else None,
            "startMs": int(start.timestamp()*1000), "endMs": int(end.timestamp()*1000),
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
            "skills": topn(st["skill"], 16), "domains": topn(st["domain"], 14),
            "searchKw": topn(st["kw"], 24), "files": topn(st["file"], 12),
            "locAdd": st["locAdd"], "locDel": st["locDel"],
            "writes": st["writes"], "writeLines": st["writeLines"],
            "corrections": corr, "interrupts": interr, "compaction": compact, "corrEx": corr_ex,
            "errSig": topn(st["errsig"], 12),
            "subs": [{"m":x["model"],"in":x["in"],"out":x["out"],"cr":x["cr"],"cc":x["cc"],
                      "nt":x["nTools"],"nw":x["nWrites"],"t0":x["t0"],"t1":x["t1"]}
                     for x in sorted(st["subs"], key=lambda x:-(x["in"]+x["out"]))[:50]],
            "modelTok": {m: v for m, v in st["modelTok"].items()},
            "hd": {k: round(v) for k,v in hd.items() if v >= 1},
            "cal": {k: round(v) for k,v in cal.items() if v >= 1},
        })
    sessions.sort(key=lambda s: s["start"])
    return sessions

def attach_commits(sessions, grace_ms=45*60*1000):
    """Correlate each session with commits in its local git repo, matched by
    repo + time window. Read-only (git log / rev-list). Sessions whose cwd isn't
    a git repo get hasRepo=False. The raw `_cwd` is consumed and removed here."""
    root_cache = {}
    def root_of(cwd):
        if not cwd: return None
        if cwd in root_cache: return root_cache[cwd]
        r = git(["rev-parse","--show-toplevel"], cwd).strip() or None
        root_cache[cwd] = r; return r
    # group sessions by repo root
    by_repo = defaultdict(list)
    for s in sessions:
        r = root_of(s.get("_cwd"))
        s["hasRepo"] = bool(r)
        if r: by_repo[r].append(s)
    for repo, sess in by_repo.items():
        main_ref = next((b for b in ("main","master") if git(["rev-parse","--verify","-q",b], repo).strip()), None)
        merged = set(git(["rev-list", main_ref], repo).split()) if main_ref else set()
        since = datetime.fromtimestamp(min(s["startMs"] for s in sess)/1000).strftime("%Y-%m-%d")
        raw = git(["log","--all","--no-merges",f"--since={since}","--pretty=format:\x01%H|%at|%s","--numstat"], repo)
        commits = []
        cur = None
        for line in raw.split("\n"):
            if line.startswith("\x01"):
                if cur: commits.append(cur)
                try: h, at, sub = line[1:].split("|", 2)
                except ValueError: cur = None; continue
                cur = {"h":h, "at":int(at)*1000, "sub":sub, "a":0, "d":0, "files":[]}
            elif line.strip() and cur:
                parts = line.split("\t")
                if len(parts) == 3 and parts[0].isdigit():
                    a = int(parts[0]); d = int(parts[1]) if parts[1].isdigit() else 0
                    cur["a"] += a; cur["d"] += d; cur["files"].append((parts[2], a, d))
        if cur: commits.append(cur)
        # short-term churn proxy (GitClear-style): lines deleted from a file that
        # were added to it within the prior 14 days = rework/throwaway, not durable.
        WINDOW = 14*86400*1000
        recent = defaultdict(list)  # path -> [[time, adds_remaining], ...]
        for c in sorted(commits, key=lambda c: c["at"]):
            c["churn"] = 0
            for path, a, d in c["files"]:
                dq = recent[path]
                while dq and dq[0][0] < c["at"]-WINDOW: dq.pop(0)
                rem = d
                while rem > 0 and dq:
                    take = min(rem, dq[0][1]); c["churn"] += take; rem -= take; dq[0][1] -= take
                    if dq[0][1] <= 0: dq.pop(0)
                if a > 0: dq.append([c["at"], a])
        # active-date set per session (from idle-capped cal buckets) — avoids a
        # stale long-span session vacuuming up commits on days it wasn't working
        for s in sess: s["_days"] = set(s.get("cal", {}).keys())
        for c in commits:
            c["merged"] = c["h"] in merged
            cdate = datetime.fromtimestamp(c["at"]/1000).strftime("%Y-%m-%d")
            cand = [s for s in sess if cdate in s["_days"]]
            if not cand: continue
            # prefer a session whose actual window brackets the commit; else latest-starting
            contain = [s for s in cand if s["startMs"] <= c["at"] <= s["endMs"]+grace_ms]
            pick = max(contain or cand, key=lambda s: s["startMs"])
            pick.setdefault("_commits", []).append(c)
    for s in sessions:
        cs = s.pop("_commits", [])
        s.pop("_cwd", None); s.pop("_days", None)
        if cs:
            s["commits"] = {"n": len(cs),
                            "add": sum(c["a"] for c in cs), "del": sum(c["d"] for c in cs),
                            "churn": sum(c.get("churn",0) for c in cs),
                            "merged": sum(1 for c in cs if c["merged"]),
                            "subjects": [c["sub"][:80] for c in sorted(cs, key=lambda c:-(c["a"]+c["d"]))[:5]]}
        else:
            s["commits"] = None
    return sessions

def main():
    ap = argparse.ArgumentParser(description="Build session analytics data.js from Claude Code transcripts.")
    ap.add_argument("--root", default=os.environ.get("CLAUDE_PROJECTS_DIR", os.path.join(HOME, ".claude", "projects")),
                    help="Directory containing Claude Code project transcripts (default: ~/.claude/projects or $CLAUDE_PROJECTS_DIR).")
    ap.add_argument("--out", default=os.path.join(SCRIPT_DIR, "data.js"),
                    help="Output JS file (default: data.js next to this script).")
    args = ap.parse_args()

    root = os.path.expanduser(args.root)
    if not os.path.isdir(root):
        print(f"error: projects dir not found: {root}", file=sys.stderr); sys.exit(1)

    all_jsonl, top_sessions, file_parent = discover(root)
    print(f"root={root}  top-level sessions={len(top_sessions)}  total files={len(all_jsonl)}", file=sys.stderr)
    valid_parents = {sid for _, _, sid in top_sessions}
    stats = content_stats(root, file_parent, valid_parents)
    sessions = session_metrics(root, top_sessions, stats)
    attach_commits(sessions)

    out = {"generatedAt": datetime.now().astimezone().isoformat(),
           "idleCapSec": IDLE_CAP, "topTools": TOP_TOOLS, "pricing": PRICING,
           "nSessions": len(sessions), "sessions": sessions}
    with open(args.out, "w") as fh:
        fh.write("const DATA = "); json.dump(out, fh, separators=(",",":")); fh.write(";\n")

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
          f"writes={sum(s['writes'] for s in sessions)} -> {args.out} ({os.path.getsize(args.out)} bytes)", file=sys.stderr)

if __name__ == "__main__":
    main()
