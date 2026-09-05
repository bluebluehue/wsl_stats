#!/usr/bin/env python3
"""
Build involvement_live.json for matches plausibly in progress.

This is intentionally lightweight:
- reads existing fixtures/transformed player data
- contacts the public Stats Perform widget feed only for current match candidates
- writes a small static snapshot consumed by GitHub Pages
- does NOT run the full fantasy/odds pipeline

The frontend's "Refresh Latest" button only reloads this JSON file.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
FIXTURES_FILE = ROOT / "fixtures.json"
PLAYERS_FILE = ROOT / "transformed_data.json"
OUTPUT_FILE = ROOT / "involvement_live.json"

OPTA_WIDGET_FEED_ID = os.getenv("OPTA_WIDGET_FEED_ID", "ft1tiv1inq7v1sk3y9tv12yh5")
OPTA_BASE = "https://api.performfeeds.com/soccerdata/matchevent"
REFERER = "https://optaplayerstats.statsperform.com/"

# Include a little pre-kickoff grace and a long enough post-kickoff window to
# cover regulation time, stoppage, and feed lag. Completed matches are harmless:
# the frontend prefers the newest snapshot until permanent history catches up.
PRE_KICKOFF_MINUTES = 5
POST_KICKOFF_HOURS = 4

def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

def compact_id(value):
    text = str(value or "")
    return text.split(":")[-1] if ":" in text else text

def parse_utc(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def qualifier_ids(event):
    quals = event.get("qualifier") or event.get("qualifiers") or []
    out = set()
    for q in quals if isinstance(quals, list) else []:
        try:
            out.add(int(q.get("qualifierId")))
        except (TypeError, ValueError, AttributeError):
            pass
    return out

def event_player_id(event):
    for key in ("playerId", "participantId", "player_id", "participant_id"):
        if event.get(key):
            return compact_id(event[key])
    return ""

def is_key_pass(event):
    return any(event.get(k) in (1, "1", True, "true", "True")
               for k in ("keypass", "keyPass", "key_pass"))

def player_lookup(players):
    if isinstance(players, dict):
        players = players.get("data") or players.get("players") or []
    lookup = {}
    for p in players:
        oid = compact_id(p.get("Opta Player ID") or p.get("opta_player_id") or p.get("optaPlayerId"))
        if oid:
            lookup[oid] = p
    return lookup

def fetch_match(opta_id):
    url = f"{OPTA_BASE}/{OPTA_WIDGET_FEED_ID}/{opta_id}"
    params = {"_rt": "c", "_lcl": "en", "_fmt": "json", "sps": "widgets"}
    headers = {
        "Referer": REFERER,
        "User-Agent": "Mozilla/5.0 (compatible; WSL involvement snapshot)",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(url, params=params, headers=headers, timeout=25)
    r.raise_for_status()
    text = r.text.strip()

    # Be tolerant if the endpoint still wraps the response despite _fmt=json.
    try:
        return r.json()
    except Exception:
        m = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", text, flags=re.S)
        if not m:
            raise RuntimeError(f"Unexpected Stats Perform response: {text[:200]}")
        return json.loads(m.group(1))

def aggregate(payload, fixture, lookup):
    events = ((payload.get("liveData") or {}).get("event") or [])
    by_player = {}

    def counts():
        return {
            "shots_on_target": 0, "key_passes": 0, "successful_crosses": 0,
            "successful_dribbles": 0, "tackles_won": 0, "interceptions": 0,
            "clearances": 0, "blocks": 0, "recoveries": 0,
        }

    for e in events:
        try:
            typ = int(e.get("typeId"))
        except (TypeError, ValueError):
            continue
        pid = event_player_id(e)
        if not pid:
            continue
        row = by_player.setdefault(pid, counts())
        qids = qualifier_ids(e)
        try:
            outcome = int(e.get("outcome") or 0)
        except (TypeError, ValueError):
            outcome = 0

        if typ == 15:
            row["shots_on_target"] += 1
        elif typ == 16 and 28 not in qids:
            row["shots_on_target"] += 1

        if typ == 1 and is_key_pass(e):
            row["key_passes"] += 1
        if typ == 1 and outcome == 1 and 2 in qids:
            row["successful_crosses"] += 1
        if typ == 3 and outcome == 1:
            row["successful_dribbles"] += 1
        if typ == 7 and outcome == 1:
            row["tackles_won"] += 1
        if typ == 8:
            row["interceptions"] += 1
        if typ == 12:
            row["clearances"] += 1
        if typ == 10 and 94 in qids:
            row["blocks"] += 1
        if typ == 49:
            row["recoveries"] += 1

    rows = []
    for oid, c in by_player.items():
        p = lookup.get(oid, {})
        att = c["shots_on_target"] + c["key_passes"] + c["successful_crosses"] + c["successful_dribbles"]
        deff = c["tackles_won"] + c["interceptions"] + c["clearances"] + c["blocks"] + c["recoveries"]
        rows.append({
            "opta_player_id": oid,
            "player_id": p.get("Player ID") or p.get("player_id"),
            "name": p.get("Name") or f"Opta player {oid}",
            "club": p.get("Club"),
            "league": p.get("League"),
            "position": p.get("Position"),
            **c,
            "attacking_actions": att,
            "attacking_points": att // 4,
            "defensive_actions": deff,
            "defensive_points": deff // 10,
            "involvement_points": (att // 4) + (deff // 10),
        })

    details = (payload.get("liveData") or {}).get("matchDetails") or {}
    info = payload.get("matchInfo") or {}
    status = str(details.get("matchStatus") or details.get("status") or "").strip()
    lower = status.lower()
    final = ("played" in lower or "full" in lower or lower == "ft" or "finished" in lower)

    minutes = []
    for e in events:
        try:
            minutes.append(int(e.get("timeMin")))
        except (TypeError, ValueError):
            pass

    return {
        "match_id": fixture.get("match_id"),
        "date": fixture.get("match_date_time_utc") or fixture.get("fixture_date_iso"),
        "opta_match_id": compact_id(fixture.get("provider_id")),
        "home_id": fixture.get("home_id"),
        "away_id": fixture.get("away_id"),
        "competition_id": fixture.get("competition_id"),
        "league_game_week": fixture.get("league_game_week") or fixture.get("game_week"),
        "fantasy_game_week": fixture.get("fantasy_game_week"),
        "description": info.get("description") or f"{fixture.get('home_name') or fixture.get('home_id')} vs {fixture.get('away_name') or fixture.get('away_id')}",
        "match_status": status or ("Final" if final else "In progress"),
        "in_progress": bool(events) and not final,
        "minute": max(minutes) if minutes else None,
        "players": rows,
        "event_count": len(events),
    }

def main():
    fixtures = load_json(FIXTURES_FILE)
    players = load_json(PLAYERS_FILE)
    lookup = player_lookup(players)
    now = datetime.now(timezone.utc)

    candidates = []
    for f in fixtures:
        oid = compact_id(f.get("provider_id"))
        kickoff = parse_utc(f.get("match_date_time_utc") or f.get("deadline_date") or f.get("fixture_date_iso"))
        if not oid or not kickoff:
            continue
        if kickoff - timedelta(minutes=PRE_KICKOFF_MINUTES) <= now <= kickoff + timedelta(hours=POST_KICKOFF_HOURS):
            candidates.append(f)

    matches = []
    failures = []
    for f in candidates:
        oid = compact_id(f.get("provider_id"))
        try:
            payload = fetch_match(oid)
            if not isinstance(payload, dict) or "liveData" not in payload:
                failures.append({"opta_match_id": oid, "error": f"Feed error: {payload}"})
                continue
            match = aggregate(payload, f, lookup)
            if match["event_count"] > 0:
                matches.append(match)
        except Exception as exc:
            failures.append({"opta_match_id": oid, "error": str(exc)})

    snapshot = {
        "metadata": {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "refresh_interval_minutes": 15,
            "candidate_match_count": len(candidates),
            "match_count": len(matches),
            "failures": failures,
        },
        "matches": matches,
    }
    write_json(OUTPUT_FILE, snapshot)
    print(f"Wrote {OUTPUT_FILE.name}: {len(matches)} matches, {len(failures)} failures.")

if __name__ == "__main__":
    main()
