#!/usr/bin/env python3
import json, os, re
from pathlib import Path
from datetime import datetime, timezone

OUT = Path("involvement_definition_audit.json")
CACHE = Path("raw_opta_events")
TRANSFORMED = Path("transformed_data.json")

GROUND_TRUTH = [
 ("Lucy Bronze",6,9), ("Jade Richards",0,17),
 ("Maria Pilar León Cebrián",3,13), ("Katie McCabe",8,9),
 ("Alyssa Thompson",8,7), ("Hannah Hampton",0,6),
 ("Alexia Putellas",3,2), ("Daniëlle van de Donk",1,6),
 ("Claudia Mummery-Walker",2,2), ("Safia Middleton-Patel",0,18),
 ("Lauren James",12,6), ("Keira Walsh",4,9), ("Grace Geyoro",2,10),
 ("Emma Siddall",6,8), ("Maddi Wilde",3,15), ("Andrea Medina",0,21),
 ("Poppy Wilson",3,6), ("Coral-Jade Haines",5,5),
]
ALIASES = {
 "Maria Pilar León Cebrián":["Mapi León","Mapi Leon"],
 "Daniëlle van de Donk":["Danielle van de Donk"],
 "Claudia Mummery-Walker":["Claudia Walker"],
}

def load(p, default=None):
    try: return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception: return default

def norm(s):
    s=str(s or "").lower()
    tr=str.maketrans("áàäâãåéèëêíìïîóòöôõúùüûñç","aaaaaaeeeeiiiiooooouuuunc")
    return re.sub(r"[^a-z0-9]+"," ",s.translate(tr)).strip()

def rows(d):
    if isinstance(d,list): return d
    if isinstance(d,dict):
        for k in ("players","data","results"):
            if isinstance(d.get(k),list): return d[k]
    return []

def pname(p):
    for k in ("Player","player_name","name","fullName","displayName"):
        if p.get(k): return str(p[k])
    return (str(p.get("firstName",""))+" "+str(p.get("lastName",""))).strip()

def pids(p):
    out=[]
    for k,v in p.items():
        kl=str(k).lower()
        if ("opta" in kl or kl in ("id","playerid","player_id")) and isinstance(v,(str,int)):
            out.append(str(v).replace("opta:player:",""))
    return list(dict.fromkeys(out))

def find_player(name, ps):
    wants={norm(name),*(norm(x) for x in ALIASES.get(name,[]))}
    for p in ps:
        if norm(pname(p)) in wants: return p
    return None

def find_events(x):
    if isinstance(x,dict):
        ld=x.get("liveData")
        if isinstance(ld,dict) and isinstance(ld.get("event"),list): return ld["event"]
        for v in x.values():
            z=find_events(v)
            if z is not None: return z
    elif isinstance(x,list):
        for v in x:
            z=find_events(v)
            if z is not None: return z
    return None

def eid(e):
    for k in ("playerId","player_id","playerID"):
        if e.get(k) not in (None,""): return str(e[k]).replace("opta:player:","")
    p=e.get("player")
    if isinstance(p,dict) and p.get("id") is not None: return str(p["id"]).replace("opta:player:","")
    return None

def typ(e):
    v=e.get("typeId",e.get("type_id"))
    if v is None and isinstance(e.get("type"),dict): v=e["type"].get("id")
    try:return int(v)
    except:return None

def outcome(e):
    v=e.get("outcome",e.get("outcomeId",e.get("outcome_id")))
    try:return int(v)
    except:return v

def quals(e):
    q=e.get("qualifier",e.get("qualifiers",[])) or []
    if isinstance(q,dict): q=[q]
    out=[]
    for x in q:
        if not isinstance(x,dict): continue
        i=x.get("qualifierId",x.get("id",x.get("typeId")))
        try:i=int(i)
        except:pass
        out.append({"id":i,"value":x.get("value",x.get("qualifierValue"))})
    return out

def qids(e): return {x["id"] for x in quals(e)}

def keypass(e):
    return any(e.get(k) in (True,1,"1","true","True") for k in ("keypass","keyPass","key_pass"))

def classes(e):
    t,o,qs=typ(e),outcome(e),qids(e); c=[]
    if t==15 or (t==16 and 28 not in qs): c+=["shot_on_target"]
    if t==1 and keypass(e): c+=["key_pass"]
    if t==1 and o==1 and 2 in qs: c+=["successful_cross"]
    if t==3 and o==1: c+=["successful_dribble"]
    if t==7 and o==1: c+=["tackle_won"]
    if t==8: c+=["interception"]
    if t==12: c+=["clearance"]
    if t==10 and 94 in qs: c+=["blocked_shot"]
    if t==49: c+=["recovery"]
    return c

def compact(e):
    return {
      "event_id":e.get("eventId",e.get("id")), "type_id":typ(e), "outcome":outcome(e),
      "period":e.get("periodId",e.get("period")), "minute":e.get("timeMin",e.get("minute")),
      "second":e.get("timeSec",e.get("second")), "x":e.get("x"), "y":e.get("y"),
      "keypass":keypass(e), "qualifiers":quals(e), "current_classification":classes(e)
    }

ps=rows(load(TRANSFORMED,{}))
matches=[]
for f in CACHE.glob("*.json") if CACHE.exists() else []:
    d=load(f,{})
    matches.append((f.stem,find_events(d) or []))

results=[]; unmatched=[]
for name,oa,od in GROUND_TRUTH:
    p=find_player(name,ps)
    if not p:
        unmatched.append(name); continue
    ids=set(pids(p)); evs=[]; mids=[]
    for mid,all_e in matches:
        pe=[e for e in all_e if eid(e) in ids]
        if pe: evs+=pe; mids.append(mid)
    cnt={k:0 for k in ("shots_on_target","key_passes","successful_crosses","successful_dribbles",
                       "tackles_won","interceptions","clearances","blocked_shots","recoveries")}
    mp={"shot_on_target":"shots_on_target","key_pass":"key_passes","successful_cross":"successful_crosses",
        "successful_dribble":"successful_dribbles","tackle_won":"tackles_won","interception":"interceptions",
        "clearance":"clearances","blocked_shot":"blocked_shots","recovery":"recoveries"}
    for e in evs:
        for c in classes(e): cnt[mp[c]]+=1
    att=sum(cnt[k] for k in ("shots_on_target","key_passes","successful_crosses","successful_dribbles"))
    de=sum(cnt[k] for k in ("tackles_won","interceptions","clearances","blocked_shots","recoveries"))
    types={}
    for e in evs: types[str(typ(e))]=types.get(str(typ(e)),0)+1
    results.append({
      "player":name,"matched_name":pname(p),"player_ids":sorted(ids),"match_ids":mids,
      "official_wsl_ui":{"attacking_actions":oa,"defensive_actions":od},
      "current_parser":{**cnt,"attacking_actions":att,"defensive_actions":de,
                        "att_delta_vs_wsl":att-oa,"def_delta_vs_wsl":de-od},
      "all_event_type_counts":types,
      "candidate_events":[compact(e) for e in evs if typ(e) in {1,3,7,8,10,12,13,14,15,16,32,49}]
    })

payload={
 "metadata":{"generated_at":datetime.now(timezone.utc).isoformat(),
             "purpose":"Diagnostic reconciliation against official WSL Fantasy UI; production unchanged.",
             "players_audited":len(results),"unmatched":unmatched,
             "exact_att":sum(r["current_parser"]["att_delta_vs_wsl"]==0 for r in results),
             "exact_def":sum(r["current_parser"]["def_delta_vs_wsl"]==0 for r in results)},
 "ground_truth":[{"player":n,"attacking_actions":a,"defensive_actions":d} for n,a,d in GROUND_TRUTH],
 "players":results
}
OUT.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8")
print("Wrote",OUT)
print(json.dumps(payload["metadata"],indent=2,ensure_ascii=False))
