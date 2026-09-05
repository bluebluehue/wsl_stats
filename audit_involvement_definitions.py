#!/usr/bin/env python3
"""
WSL Fantasy involvement-definition reconciliation audit v4.

This is DIAGNOSTIC ONLY. It does not modify production involvement logic.

What it does:
1) Loads transformed_data.json and fixtures.json.
2) Matches the fixed official-WSL screenshot validation players.
3) Finds season fixtures whose kickoff has already happened.
4) Uses each fixture's provider_id (opta:Match:...) to load/fetch the public
   Stats Perform matchevent feed.
5) Normalizes Opta IDs consistently (important: transformed data uses
   opta:Player:..., while event feeds may use bare IDs).
6) Dumps every event belonging to each validation player, plus:
   - current parser component totals
   - alternative event counts for hypothesis testing
   - official WSL ATT/DEF totals
   - deltas vs WSL
7) Fails loudly if zero matches or zero target-player events are found.

Output:
  involvement_definition_audit.json
"""

import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

import requests

TRANSFORMED = Path("transformed_data.json")
FIXTURES = Path("fixtures.json")
CACHE_DIR = Path("raw_opta_events")
OUT = Path("involvement_definition_audit.json")

FEED_ID = os.getenv("OPTA_WIDGET_FEED_ID", "ft1tiv1inq7v1sk3y9tv12yh5")
BASE_URL = "https://api.performfeeds.com/soccerdata/matchevent"
HEADERS = {
    "User-Agent": "Mozilla/5.0 WSL fantasy involvement definition audit",
    "Accept": "*/*",
    "Referer": "https://optaplayerstats.statsperform.com/",
}

# Exact official WSL Fantasy UI totals transcribed from screenshots.
GROUND_TRUTH = [
    {"name": "Lucy Bronze", "att": 6, "def": 9},
    {"name": "Jade Richards", "att": 0, "def": 17},
    {"name": "Maria Pilar León Cebrián", "aliases": ["Mapi León", "Mapi Leon"], "att": 3, "def": 13},
    {"name": "Katie McCabe", "att": 8, "def": 9},
    {"name": "Alyssa Thompson", "att": 8, "def": 7},
    {"name": "Hannah Hampton", "att": 0, "def": 6},
    {"name": "Alexia Putellas", "att": 3, "def": 2},
    {"name": "Daniëlle van de Donk", "aliases": ["Danielle van de Donk"], "att": 1, "def": 6},
    {"name": "Claudia Mummery-Walker", "aliases": ["Claudia Walker"], "att": 2, "def": 2},
    {"name": "Safia Middleton-Patel", "att": 0, "def": 18},
    {"name": "Lauren James", "att": 12, "def": 6},
    {"name": "Keira Walsh", "att": 4, "def": 9},
    {"name": "Grace Geyoro", "att": 2, "def": 10},
    {"name": "Emma Siddall", "att": 6, "def": 8},
    {"name": "Maddi Wilde", "att": 3, "def": 15},
    {"name": "Andrea Medina", "att": 0, "def": 21},
    {"name": "Poppy Wilson", "att": 3, "def": 6},
    {"name": "Coral-Jade Haines", "att": 5, "def": 5},
]

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def normalize_id(value):
    """Return bare final Opta/provider token, lowercased, regardless of prefix case."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if ":" in s:
        s = s.split(":")[-1]
    return s.lower()

def norm_name(value):
    s = str(value or "").lower()
    repl = str.maketrans(
        "áàäâãåéèëêíìïîóòöôõúùüûñç",
        "aaaaaaeeeeiiiiooooouuuunc"
    )
    return re.sub(r"[^a-z0-9]+", " ", s.translate(repl)).strip()

def player_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("players", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []

def player_name(p):
    for k in ("Name", "Player", "player_name", "name", "fullName", "displayName"):
        if p.get(k):
            return str(p[k])
    return (str(p.get("firstName", "")) + " " + str(p.get("lastName", ""))).strip()

def player_opta_ids(p):
    out = []
    explicit = (
        "Opta Player ID", "Opta ID", "OptaID", "opta_id", "optaId",
        "optaPlayerId", "opta_player_id"
    )
    for k in explicit:
        v = p.get(k)
        nv = normalize_id(v)
        if nv:
            out.append(nv)
    # Only accept generic player IDs if they themselves are opta-prefixed.
    for k in ("Player ID", "PlayerID", "playerId", "player_id", "ID", "id"):
        v = p.get(k)
        if isinstance(v, str) and v.lower().startswith("opta:"):
            nv = normalize_id(v)
            if nv:
                out.append(nv)
    return list(dict.fromkeys(out))

def match_player(gt, players):
    wanted = {norm_name(gt["name"])}
    wanted.update(norm_name(x) for x in gt.get("aliases", []))
    for p in players:
        if norm_name(player_name(p)) in wanted:
            return p
    return None

def parse_iso_utc(value):
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def fixture_rows(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("fixtures", "matches", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []

def match_opta_id(fixture):
    for k in ("provider_id", "providerId", "opta_match_id", "optaMatchId"):
        v = fixture.get(k)
        if v:
            return normalize_id(v)
    return None

def unwrap_jsonp(text):
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    # Strip "callback(" prefix and trailing ");"
    first = text.find("(")
    last = text.rfind(")")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("Stats Perform response was neither JSON nor recognizable JSONP")
    return json.loads(text[first + 1:last])

def fetch_match_event(opta_match_id):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{opta_match_id}.json"

    if path.exists():
        cached = load_json(path)
        if cached is not None:
            return cached, "cache"

    callback = "wslInvolvementAudit"
    url = f"{BASE_URL}/{FEED_ID}/{opta_match_id}"
    params = {
        "_rt": "c",
        "_lcl": "en",
        "_fmt": "jsonp",
        "sps": "widgets",
        "_clbk": callback,
    }
    response = requests.get(url, params=params, headers=HEADERS, timeout=30)
    response.raise_for_status()
    data = unwrap_jsonp(response.text)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data, "fetched"

def find_event_list(obj):
    if isinstance(obj, dict):
        live = obj.get("liveData")
        if isinstance(live, dict) and isinstance(live.get("event"), list):
            return live["event"]
        for v in obj.values():
            found = find_event_list(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_event_list(v)
            if found is not None:
                return found
    return None

def event_player_id(e):
    for k in ("playerId", "player_id", "playerID"):
        if e.get(k) not in (None, ""):
            return normalize_id(e[k])
    p = e.get("player")
    if isinstance(p, dict):
        for k in ("id", "playerId"):
            if p.get(k) not in (None, ""):
                return normalize_id(p[k])
    return None

def event_team_id(e):
    for k in ("contestantId", "teamId", "team_id"):
        if e.get(k) not in (None, ""):
            return normalize_id(e[k])
    return None

def type_id(e):
    v = e.get("typeId", e.get("type_id"))
    if v is None and isinstance(e.get("type"), dict):
        v = e["type"].get("id")
    try:
        return int(v)
    except Exception:
        return None

def outcome(e):
    v = e.get("outcome", e.get("outcomeId", e.get("outcome_id")))
    try:
        return int(v)
    except Exception:
        return v

def qualifiers(e):
    raw = e.get("qualifier", e.get("qualifiers", [])) or []
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        qid = q.get("qualifierId", q.get("id", q.get("typeId")))
        try:
            qid = int(qid)
        except Exception:
            pass
        out.append({
            "id": qid,
            "value": q.get("value", q.get("qualifierValue")),
        })
    return out

def qualifier_ids(e):
    return {q["id"] for q in qualifiers(e)}

def is_key_pass(e):
    for k in ("keypass", "keyPass", "key_pass"):
        if e.get(k) in (True, 1, "1", "true", "True"):
            return True
    return False

def is_own_goal(e):
    return 28 in qualifier_ids(e)

def current_classes(e):
    t = type_id(e)
    o = outcome(e)
    qs = qualifier_ids(e)
    cats = []

    if t == 15:
        cats.append("shot_on_target")
    if t == 16 and not is_own_goal(e):
        cats.append("shot_on_target")
    if t == 1 and is_key_pass(e):
        cats.append("key_pass")
    if t == 1 and o == 1 and 2 in qs:
        cats.append("successful_cross")
    if t == 3 and o == 1:
        cats.append("successful_dribble")

    if t == 7 and o == 1:
        cats.append("tackle_won")
    if t == 8:
        cats.append("interception")
    if t == 12:
        cats.append("clearance")
    if t == 10 and 94 in qs:
        cats.append("blocked_shot")
    if t == 49:
        cats.append("recovery")

    return cats

def compact_event(e):
    # Preserve all scalar top-level fields so unknown types/flags can be investigated,
    # while representing qualifiers cleanly and avoiding giant nested structures.
    scalars = {}
    for k, v in e.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            scalars[k] = v
    return {
        "event_id": e.get("eventId", e.get("id")),
        "type_id": type_id(e),
        "outcome": outcome(e),
        "player_id_normalized": event_player_id(e),
        "team_id_normalized": event_team_id(e),
        "period": e.get("periodId", e.get("period")),
        "minute": e.get("timeMin", e.get("minute", e.get("min"))),
        "second": e.get("timeSec", e.get("second", e.get("sec"))),
        "x": e.get("x"),
        "y": e.get("y"),
        "keypass_flag": is_key_pass(e),
        "qualifiers": qualifiers(e),
        "current_classification": current_classes(e),
        "raw_scalar_fields": scalars,
    }

def alt_counts(events):
    """Counts useful for testing alternative definitions without rerunning."""
    def count(pred):
        return sum(1 for e in events if pred(e))

    return {
        "type1_pass_all": count(lambda e: type_id(e) == 1),
        "type1_keypass": count(lambda e: type_id(e) == 1 and is_key_pass(e)),
        "type1_cross_all": count(lambda e: type_id(e) == 1 and 2 in qualifier_ids(e)),
        "type1_cross_outcome1": count(lambda e: type_id(e) == 1 and outcome(e) == 1 and 2 in qualifier_ids(e)),
        "type3_takeon_all": count(lambda e: type_id(e) == 3),
        "type3_takeon_outcome1": count(lambda e: type_id(e) == 3 and outcome(e) == 1),
        "type7_tackle_all": count(lambda e: type_id(e) == 7),
        "type7_tackle_outcome1": count(lambda e: type_id(e) == 7 and outcome(e) == 1),
        "type7_tackle_outcome0": count(lambda e: type_id(e) == 7 and outcome(e) == 0),
        "type8_interception_all": count(lambda e: type_id(e) == 8),
        "type8_interception_outcome1": count(lambda e: type_id(e) == 8 and outcome(e) == 1),
        "type8_interception_outcome0": count(lambda e: type_id(e) == 8 and outcome(e) == 0),
        "type10_all": count(lambda e: type_id(e) == 10),
        "type10_q94": count(lambda e: type_id(e) == 10 and 94 in qualifier_ids(e)),
        "type11_all": count(lambda e: type_id(e) == 11),
        "type12_clearance_all": count(lambda e: type_id(e) == 12),
        "type12_clearance_outcome1": count(lambda e: type_id(e) == 12 and outcome(e) == 1),
        "type12_clearance_outcome0": count(lambda e: type_id(e) == 12 and outcome(e) == 0),
        "type13_miss_all": count(lambda e: type_id(e) == 13),
        "type14_post_all": count(lambda e: type_id(e) == 14),
        "type15_attempt_saved_all": count(lambda e: type_id(e) == 15),
        "type16_goal_all": count(lambda e: type_id(e) == 16),
        "type16_non_own_goal": count(lambda e: type_id(e) == 16 and not is_own_goal(e)),
        "type32_all": count(lambda e: type_id(e) == 32),
        "type49_recovery_all": count(lambda e: type_id(e) == 49),
        "type49_recovery_outcome1": count(lambda e: type_id(e) == 49 and outcome(e) == 1),
        "type49_recovery_outcome0": count(lambda e: type_id(e) == 49 and outcome(e) == 0),
        "q94_any_type": count(lambda e: 94 in qualifier_ids(e)),
        "q82_any_type": count(lambda e: 82 in qualifier_ids(e)),
    }

# ---------- Load repo data ----------
transformed = load_json(TRANSFORMED)
fixtures_data = load_json(FIXTURES)

if transformed is None:
    raise SystemExit("ERROR: transformed_data.json could not be loaded")
if fixtures_data is None:
    raise SystemExit("ERROR: fixtures.json could not be loaded")

players = player_rows(transformed)
fixtures = fixture_rows(fixtures_data)

if not players:
    raise SystemExit("ERROR: No players found in transformed_data.json")
if not fixtures:
    raise SystemExit("ERROR: No fixtures found in fixtures.json")

# ---------- Match all validation players ----------
targets = []
unmatched = []

for gt in GROUND_TRUTH:
    p = match_player(gt, players)
    if p is None:
        unmatched.append(gt["name"])
        continue

    ids = player_opta_ids(p)
    targets.append({
        "gt": gt,
        "record": p,
        "ids": ids,
    })

if unmatched:
    raise SystemExit("ERROR: Unmatched validation players: " + ", ".join(unmatched))

missing_opta_ids = [x["gt"]["name"] for x in targets if not x["ids"]]
if missing_opta_ids:
    raise SystemExit("ERROR: Validation players missing Opta Player ID: " + ", ".join(missing_opta_ids))

# ---------- Identify already-kicked-off season fixtures ----------
now = datetime.now(timezone.utc)
past_fixtures = []

for f in fixtures:
    kickoff = parse_iso_utc(
        f.get("match_date_time_utc")
        or f.get("matchDateTimeUtc")
        or f.get("deadline_date")
    )
    mid = match_opta_id(f)

    if kickoff is None or mid is None:
        continue
    if kickoff <= now:
        past_fixtures.append((kickoff, mid, f))

past_fixtures.sort(key=lambda x: x[0])

if not past_fixtures:
    raise SystemExit("ERROR: No already-kicked-off fixtures with Opta provider IDs were found")

# ---------- Fetch/load event feeds ----------
loaded_matches = []
fetch_failures = []

for kickoff, mid, fixture in past_fixtures:
    try:
        data, source = fetch_match_event(mid)
        events = find_event_list(data)
        if not isinstance(events, list):
            raise ValueError("No liveData.event list found")
        loaded_matches.append({
            "opta_match_id": mid,
            "kickoff_utc": kickoff.isoformat(),
            "home": fixture.get("home_name", fixture.get("home_id")),
            "away": fixture.get("away_name", fixture.get("away_id")),
            "source": source,
            "event_count": len(events),
            "events": events,
        })
    except Exception as exc:
        fetch_failures.append({
            "opta_match_id": mid,
            "home": fixture.get("home_name", fixture.get("home_id")),
            "away": fixture.get("away_name", fixture.get("away_id")),
            "error": repr(exc),
        })

if not loaded_matches:
    raise SystemExit(
        "ERROR: Zero Opta match event feeds loaded. Failures: "
        + json.dumps(fetch_failures, ensure_ascii=False)
    )

# ---------- Build per-player audit ----------
results = []

for target in targets:
    gt = target["gt"]
    p = target["record"]
    ids = set(target["ids"])

    player_events = []
    match_ids = []

    for m in loaded_matches:
        found = [e for e in m["events"] if event_player_id(e) in ids]
        if found:
            player_events.extend(found)
            match_ids.append(m["opta_match_id"])

    component = {
        "shots_on_target": 0,
        "key_passes": 0,
        "successful_crosses": 0,
        "successful_dribbles": 0,
        "tackles_won": 0,
        "interceptions": 0,
        "clearances": 0,
        "blocked_shots": 0,
        "recoveries": 0,
    }

    class_to_component = {
        "shot_on_target": "shots_on_target",
        "key_pass": "key_passes",
        "successful_cross": "successful_crosses",
        "successful_dribble": "successful_dribbles",
        "tackle_won": "tackles_won",
        "interception": "interceptions",
        "clearance": "clearances",
        "blocked_shot": "blocked_shots",
        "recovery": "recoveries",
    }

    for e in player_events:
        for c in current_classes(e):
            component[class_to_component[c]] += 1

    att = (
        component["shots_on_target"]
        + component["key_passes"]
        + component["successful_crosses"]
        + component["successful_dribbles"]
    )
    deff = (
        component["tackles_won"]
        + component["interceptions"]
        + component["clearances"]
        + component["blocked_shots"]
        + component["recoveries"]
    )

    type_counts = {}
    qualifier_counts_by_type = {}

    for e in player_events:
        t = str(type_id(e))
        type_counts[t] = type_counts.get(t, 0) + 1
        qualifier_counts_by_type.setdefault(t, {})
        for q in qualifiers(e):
            qk = str(q["id"])
            qualifier_counts_by_type[t][qk] = qualifier_counts_by_type[t].get(qk, 0) + 1

    results.append({
        "player": gt["name"],
        "matched_name": player_name(p),
        "opta_player_ids_normalized": sorted(ids),
        "club": p.get("Club Name", p.get("Club")),
        "position": p.get("Position"),
        "match_ids": match_ids,
        "event_count": len(player_events),
        "official_wsl_ui": {
            "attacking_actions": gt["att"],
            "defensive_actions": gt["def"],
        },
        "current_parser": {
            **component,
            "attacking_actions": att,
            "defensive_actions": deff,
            "att_delta_vs_wsl": att - gt["att"],
            "def_delta_vs_wsl": deff - gt["def"],
        },
        "alternative_counts": alt_counts(player_events),
        "all_event_type_counts": type_counts,
        "qualifier_counts_by_event_type": qualifier_counts_by_type,
        "events": [compact_event(e) for e in player_events],
    })

players_with_zero_events = [r["player"] for r in results if r["event_count"] == 0]

# This is intentionally a hard failure. A "successful" audit with no events is misleading.
if len(players_with_zero_events) == len(results):
    raise SystemExit(
        "ERROR: All 18 validation players matched transformed_data.json but ZERO Opta events "
        "matched their Opta IDs. This indicates an ID/schema issue, so no audit JSON was written."
    )

metadata = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "purpose": "Granular Opta-vs-official-WSL involvement definition reconciliation; diagnostic only.",
    "production_changed": False,
    "validation_players": len(results),
    "players_with_events": sum(r["event_count"] > 0 for r in results),
    "players_with_zero_events": players_with_zero_events,
    "loaded_matches": [
        {k: m[k] for k in ("opta_match_id", "kickoff_utc", "home", "away", "source", "event_count")}
        for m in loaded_matches
    ],
    "match_fetch_failures": fetch_failures,
    "exact_att": sum(
        r["event_count"] > 0 and r["current_parser"]["att_delta_vs_wsl"] == 0
        for r in results
    ),
    "exact_def": sum(
        r["event_count"] > 0 and r["current_parser"]["def_delta_vs_wsl"] == 0
        for r in results
    ),
}

payload = {
    "metadata": metadata,
    "ground_truth": GROUND_TRUTH,
    "players": results,
}

OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"Wrote {OUT}")
print(json.dumps(metadata, indent=2, ensure_ascii=False))

# Helpful sanity checks in Actions log.
print("\nPer-player sanity:")
for r in results:
    print(
        f'{r["player"]}: events={r["event_count"]}, matches={len(r["match_ids"])}, '
        f'ATT={r["current_parser"]["attacking_actions"]}/{r["official_wsl_ui"]["attacking_actions"]}, '
        f'DEF={r["current_parser"]["defensive_actions"]}/{r["official_wsl_ui"]["defensive_actions"]}'
    )
