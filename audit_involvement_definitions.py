#!/usr/bin/env python3
"""
WSL Fantasy involvement-definition reconciliation audit v5.

DIAGNOSTIC ONLY. This script does NOT modify production involvement logic.

It deliberately reuses the SAME match-discovery, Opta-player mapping, public
Stats Perform fetching, JSONP parsing, and current action definitions as the
working production fetch_involvement_points.py supplied on 2026-09-05.

It then layers on:
- Official WSL Fantasy screenshot ground truth for the current MW1 control set.
- Full target-player event dumps.
- Current production-parser totals.
- Candidate alternative counts (q82 blocked shots, non-PASS keyPass flags,
  Type 10/52/54/59/74 GK/defensive events, outcome variants, etc.).
- A "revised_att_hypothesis" that is reported ONLY as a diagnostic:
    SOT = Type 15 excluding qualifier 82 + non-own-goal Type 16
    KP  = any event carrying keyPass/keypass/key_pass
  The production parser is NOT changed.
- Hard failures if player matching, match loading, or event matching silently fail.

Inputs:
  transformed_data.json
  fixtures.json
  raw_feeds/players.json

Output:
  involvement_definition_audit.json

Optional env:
  OPTA_WIDGET_FEED_ID
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
TRANSFORMED_PATH = ROOT / "transformed_data.json"
FIXTURES_PATH = ROOT / "fixtures.json"
RAW_PLAYERS_PATH = ROOT / "raw_feeds" / "players.json"
OUTPUT_PATH = ROOT / "involvement_definition_audit.json"
RAW_EVENT_DIR = ROOT / "raw_opta_events"

DEFAULT_WIDGET_FEED_ID = "ft1tiv1inq7v1sk3y9tv12yh5"
WIDGET_FEED_ID = os.getenv("OPTA_WIDGET_FEED_ID", DEFAULT_WIDGET_FEED_ID)
PERFORMFEEDS_BASE = "https://api.performfeeds.com/soccerdata/matchevent"

# Production event IDs
PASS = 1
TAKE_ON = 3
TACKLE = 7
INTERCEPTION = 8
DEFENSIVE_SAVE_OR_BLOCK = 10
CLEARANCE = 12
ATTEMPT_SAVED = 15
GOAL = 16
BALL_RECOVERY_COMMON = 49
BALL_RECOVERY_ALT = 32

# Additional diagnostic event IDs seen in public Opta feeds
KEEPER_PICKUP = 52
KEEPER_SMOTHER = 54
TYPE_59 = 59
TYPE_61 = 61
BLOCKED_PASS = 74

# Qualifiers
CROSS_QUALIFIER = 2
OWN_GOAL_QUALIFIER = 28
SHOT_BLOCKED_QUALIFIER = 82
DEF_BLOCK_QUALIFIER = 94

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 WSL fantasy involvement definition audit",
    "Accept": "*/*",
    "Referer": "https://optaplayerstats.statsperform.com/",
}

# Official WSL Fantasy UI values from screenshot controls.
# Only ATT/DEF are required for reconciliation; other popup categories live in
# the companion spreadsheet and are intentionally not duplicated here.
GROUND_TRUTH = [
    # Chelsea 1-1 Aston Villa
    {"name": "Lucy Bronze", "club": "Chelsea", "att": 6, "def": 9},
    {"name": "Katie McCabe", "club": "Chelsea", "att": 8, "def": 9},
    {"name": "Alyssa Thompson", "club": "Chelsea", "att": 8, "def": 7},
    {"name": "Hannah Hampton", "club": "Chelsea", "att": 0, "def": 6},
    {"name": "Lauren James", "club": "Chelsea", "att": 12, "def": 6},
    {"name": "Keira Walsh", "club": "Chelsea", "att": 4, "def": 9},
    {"name": "Ellie Carpenter", "club": "Chelsea", "att": 6, "def": 4},
    {"name": "Akane Okuma", "club": "Aston Villa", "att": 0, "def": 11},
    {"name": "Noëlle Maritz", "aliases": ["Noelle Maritz"], "club": "Aston Villa", "att": 3, "def": 12},
    {"name": "Lucia Kendall", "club": "Aston Villa", "att": 0, "def": 4},
    {"name": "Lynn Wilms", "club": "Aston Villa", "att": 1, "def": 9},
    {"name": "Mia McAulay", "club": "Aston Villa", "att": 1, "def": 3},

    # London City Lionesses 2-1 Manchester United
    {"name": "Maria Pilar León Cebrián", "aliases": ["Mapi León", "Mapi Leon"], "club": "London City Lionesses", "att": 3, "def": 13},
    {"name": "Alexia Putellas", "club": "London City Lionesses", "att": 3, "def": 2},
    {"name": "Daniëlle van de Donk", "aliases": ["Danielle van de Donk"], "club": "London City Lionesses", "att": 1, "def": 6},
    {"name": "Grace Geyoro", "club": "London City Lionesses", "att": 2, "def": 10},
    {"name": "Elene Lete", "club": "London City Lionesses", "att": 0, "def": 4},
    {"name": "Alanna Kennedy", "club": "London City Lionesses", "att": 2, "def": 14},
    {"name": "Lucía Corrales", "aliases": ["Lucia Corrales"], "club": "London City Lionesses", "att": 2, "def": 9},

    {"name": "Phallon Tullis-Joyce", "club": "Manchester United", "att": 0, "def": 13},
    {"name": "Jayde Riviere", "club": "Manchester United", "att": 1, "def": 3},
    {"name": "Maya Le Tissier", "club": "Manchester United", "att": 2, "def": 13},
    {"name": "Simi Awujo", "club": "Manchester United", "att": 1, "def": 4},
    {"name": "Hinata Miyazawa", "club": "Manchester United", "att": 0, "def": 17},
    {"name": "Jess Park", "club": "Manchester United", "att": 3, "def": 3},
    {"name": "Anna Sandberg", "club": "Manchester United", "att": 0, "def": 3},

    # Watford 0-0 Burnley
    {"name": "Safia Middleton-Patel", "club": "Watford", "att": 0, "def": 18},
    {"name": "Zara Bailey", "club": "Watford", "att": 1, "def": 6},
    {"name": "Poppy Wilson", "club": "Watford", "att": 3, "def": 6},
    {"name": "Coral-Jade Haines", "club": "Watford", "att": 5, "def": 5},

    {"name": "Kirstie Levell", "club": "Burnley", "att": 0, "def": 11},
    {"name": "Jade Richards", "club": "Burnley", "att": 0, "def": 17},
    {"name": "Emma Siddall", "club": "Burnley", "att": 6, "def": 8},
    {"name": "Maddi Wilde", "club": "Burnley", "att": 3, "def": 15},
    {"name": "Andrea Medina", "club": "Burnley", "att": 0, "def": 21},
    {"name": "Alethea Paul", "club": "Burnley", "att": 0, "def": 9},
    {"name": "Claudia Mummery-Walker", "aliases": ["Claudia Walker"], "club": "Burnley", "att": 2, "def": 2},
]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def value_of(feed: dict[str, Any]) -> Any:
    return feed.get("Data", {}).get("Value")


def compact_opta_id(value: Any) -> str:
    text = str(value or "")
    return text.rsplit(":", 1)[-1]


def strip_jsonp(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", text, flags=re.S)
    if not match:
        raise ValueError("Response was neither JSON nor recognizable JSONP.")
    return json.loads(match.group(1))


def fetch_match_events(opta_match_id: str, force: bool = False) -> tuple[dict[str, Any], str]:
    RAW_EVENT_DIR.mkdir(exist_ok=True)
    cache_path = RAW_EVENT_DIR / f"{opta_match_id}.json"

    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8")), "cache"

    params = {
        "_rt": "c",
        "_lcl": "en",
        "_fmt": "jsonp",
        "sps": "widgets",
        "_clbk": "wslFantasyInvolvementAudit",
    }
    url = f"{PERFORMFEEDS_BASE}/{WIDGET_FEED_ID}/{opta_match_id}"
    response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=45)
    response.raise_for_status()
    payload = strip_jsonp(response.text)

    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload, "fetched"


def qualifiers(event: dict[str, Any]) -> list[dict[str, Any]]:
    q = event.get("qualifier") or event.get("qualifiers") or []
    if isinstance(q, list):
        return [x for x in q if isinstance(x, dict)]
    if isinstance(q, dict):
        return [q]
    return []


def qualifier_ids(event: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for q in qualifiers(event):
        raw = q.get("qualifierId", q.get("id", q.get("typeId")))
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def event_player_id(event: dict[str, Any]) -> str:
    for key in ("playerId", "participantId", "player_id", "participant_id"):
        if event.get(key):
            return compact_opta_id(event.get(key))
    return ""


def event_type(event: dict[str, Any]) -> int | None:
    raw = event.get("typeId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def event_outcome(event: dict[str, Any]) -> int:
    raw = event.get("outcome")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def is_key_pass(event: dict[str, Any]) -> bool:
    for key in ("keypass", "keyPass", "key_pass"):
        value = event.get(key)
        if value in (1, "1", True, "true", "True"):
            return True
    return False


def live_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    live = payload.get("liveData") or {}
    events = live.get("event") or []
    return events if isinstance(events, list) else []


def match_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    mi = payload.get("matchInfo") or {}
    live = payload.get("liveData") or {}
    md = live.get("matchDetails") or {}
    return {
        "description": mi.get("description"),
        "date": mi.get("date") or mi.get("localDate"),
        "match_status": md.get("matchStatus"),
        "coverage_level": mi.get("coverageLevel"),
        "last_updated": mi.get("lastUpdated"),
    }


def build_completed_match_map(
    raw_players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Exact production discovery logic:
    completed matches come from raw player match arrays because fixtures can lag.
    """
    seen: dict[str, dict[str, Any]] = {}

    for player in raw_players:
        for match in player.get("matches", []) or []:
            match_id = str(match.get("matchId") or "")
            if not match_id:
                continue

            status = match.get("matchdayStatus")
            try:
                completed = int(status) == 5
            except (TypeError, ValueError):
                completed = False

            if not completed:
                continue

            seen.setdefault(match_id, {
                "match_id": match_id,
                "date": match.get("matchDateTimeUtc"),
                "matchday_id": match.get("matchdayId"),
            })

    fixture_by_id = {str(f.get("match_id") or ""): f for f in fixtures}

    out = []
    for match_id, row in seen.items():
        fixture = fixture_by_id.get(match_id, {})
        provider_id = fixture.get("provider_id")
        opta_match_id = compact_opta_id(provider_id)
        if not opta_match_id:
            continue

        out.append({
            **row,
            "opta_match_id": opta_match_id,
            "home_id": fixture.get("home_id"),
            "away_id": fixture.get("away_id"),
            "competition_id": fixture.get("competition_id"),
            "league_game_week": fixture.get("league_game_week") or fixture.get("game_week"),
            "fantasy_game_week": fixture.get("fantasy_game_week"),
        })

    return sorted(out, key=lambda r: (str(r.get("date") or ""), r["opta_match_id"]))


def normalize_name(value: Any) -> str:
    s = str(value or "").casefold()
    translation = str.maketrans({
        "á":"a","à":"a","ä":"a","â":"a","ã":"a","å":"a",
        "é":"e","è":"e","ë":"e","ê":"e",
        "í":"i","ì":"i","ï":"i","î":"i",
        "ó":"o","ò":"o","ö":"o","ô":"o","õ":"o",
        "ú":"u","ù":"u","ü":"u","û":"u",
        "ñ":"n","ç":"c","ë":"e","ö":"o",
    })
    s = s.translate(translation)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def match_ground_truth_player(
    gt: dict[str, Any],
    players: list[dict[str, Any]],
) -> dict[str, Any] | None:
    wanted_names = {normalize_name(gt["name"])}
    wanted_names.update(normalize_name(x) for x in gt.get("aliases", []))
    wanted_club = normalize_name(gt.get("club"))

    candidates = []
    for p in players:
        name = normalize_name(p.get("Name"))
        if name not in wanted_names:
            continue
        candidates.append(p)

    if not candidates:
        return None

    # Club disambiguation if needed.
    if wanted_club:
        for p in candidates:
            if normalize_name(p.get("Club") or p.get("Club Name")) == wanted_club:
                return p

    return candidates[0]


def current_production_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    """
    EXACT current production classification copied from fetch_involvement_points.py.
    """
    c = Counter()

    for event in events:
        typ = event_type(event)
        if typ is None:
            continue
        qids = qualifier_ids(event)
        outcome = event_outcome(event)

        if typ == ATTEMPT_SAVED:
            c["shots_on_target"] += 1
        elif typ == GOAL and OWN_GOAL_QUALIFIER not in qids:
            c["shots_on_target"] += 1

        if typ == PASS and is_key_pass(event):
            c["key_passes"] += 1

        if typ == PASS and outcome == 1 and CROSS_QUALIFIER in qids:
            c["successful_crosses"] += 1

        if typ == TAKE_ON and outcome == 1:
            c["successful_dribbles"] += 1

        if typ == TACKLE and outcome == 1:
            c["tackles_won"] += 1

        if typ == INTERCEPTION:
            c["interceptions"] += 1

        if typ == CLEARANCE:
            c["clearances"] += 1

        if typ == DEFENSIVE_SAVE_OR_BLOCK and DEF_BLOCK_QUALIFIER in qids:
            c["blocks"] += 1

        if typ == BALL_RECOVERY_COMMON:
            c["recoveries"] += 1

        if typ == BALL_RECOVERY_ALT:
            c["recovery_type32_diagnostic"] += 1

    attacking = (
        c["shots_on_target"] + c["key_passes"] +
        c["successful_crosses"] + c["successful_dribbles"]
    )
    defensive = (
        c["tackles_won"] + c["interceptions"] + c["clearances"] +
        c["blocks"] + c["recoveries"]
    )

    return {
        "shots_on_target": c["shots_on_target"],
        "key_passes": c["key_passes"],
        "successful_crosses": c["successful_crosses"],
        "successful_dribbles": c["successful_dribbles"],
        "attacking_actions": attacking,
        "attacking_points": attacking // 4,
        "tackles_won": c["tackles_won"],
        "interceptions": c["interceptions"],
        "clearances": c["clearances"],
        "blocks": c["blocks"],
        "recoveries": c["recoveries"],
        "defensive_actions": defensive,
        "defensive_points": defensive // 10,
        "involvement_points": attacking // 4 + defensive // 10,
        "recovery_type32_diagnostic": c["recovery_type32_diagnostic"],
    }


def revised_att_hypothesis(events: list[dict[str, Any]]) -> dict[str, int]:
    """
    Diagnostic hypothesis only. Does not alter production:
    - Type 15 q82 is excluded from SOT.
    - keyPass flag can live on event types other than PASS.
    """
    c = Counter()
    for event in events:
        typ = event_type(event)
        if typ is None:
            continue
        qids = qualifier_ids(event)
        outcome = event_outcome(event)

        if typ == ATTEMPT_SAVED and SHOT_BLOCKED_QUALIFIER not in qids:
            c["shots_on_target"] += 1
        elif typ == GOAL and OWN_GOAL_QUALIFIER not in qids:
            c["shots_on_target"] += 1

        if is_key_pass(event):
            c["key_passes"] += 1

        if typ == PASS and outcome == 1 and CROSS_QUALIFIER in qids:
            c["successful_crosses"] += 1

        if typ == TAKE_ON and outcome == 1:
            c["successful_dribbles"] += 1

    total = sum(c[k] for k in (
        "shots_on_target", "key_passes", "successful_crosses", "successful_dribbles"
    ))
    return {
        "shots_on_target": c["shots_on_target"],
        "key_passes": c["key_passes"],
        "successful_crosses": c["successful_crosses"],
        "successful_dribbles": c["successful_dribbles"],
        "attacking_actions": total,
        "attacking_points": total // 4,
    }


def alternative_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    def count(pred) -> int:
        return sum(1 for e in events if pred(e))

    return {
        # ATT candidates
        "type15_all": count(lambda e: event_type(e) == 15),
        "type15_q82_blocked": count(lambda e: event_type(e) == 15 and 82 in qualifier_ids(e)),
        "type15_without_q82": count(lambda e: event_type(e) == 15 and 82 not in qualifier_ids(e)),
        "type16_non_own_goal": count(lambda e: event_type(e) == 16 and 28 not in qualifier_ids(e)),
        "keypass_any_event": count(is_key_pass),
        "keypass_type1": count(lambda e: event_type(e) == 1 and is_key_pass(e)),
        "keypass_non_type1": count(lambda e: event_type(e) != 1 and is_key_pass(e)),
        "cross_type1_all": count(lambda e: event_type(e) == 1 and 2 in qualifier_ids(e)),
        "cross_type1_successful": count(lambda e: event_type(e) == 1 and event_outcome(e) == 1 and 2 in qualifier_ids(e)),
        "takeon_all": count(lambda e: event_type(e) == 3),
        "takeon_successful": count(lambda e: event_type(e) == 3 and event_outcome(e) == 1),

        # DEF candidates
        "tackle_type7_all": count(lambda e: event_type(e) == 7),
        "tackle_type7_won": count(lambda e: event_type(e) == 7 and event_outcome(e) == 1),
        "tackle_type7_lost": count(lambda e: event_type(e) == 7 and event_outcome(e) == 0),
        "interception_type8_all": count(lambda e: event_type(e) == 8),
        "interception_type8_outcome1": count(lambda e: event_type(e) == 8 and event_outcome(e) == 1),
        "interception_type8_outcome0": count(lambda e: event_type(e) == 8 and event_outcome(e) == 0),
        "clearance_type12_all": count(lambda e: event_type(e) == 12),
        "clearance_type12_outcome1": count(lambda e: event_type(e) == 12 and event_outcome(e) == 1),
        "clearance_type12_outcome0": count(lambda e: event_type(e) == 12 and event_outcome(e) == 0),
        "type10_all": count(lambda e: event_type(e) == 10),
        "type10_q94": count(lambda e: event_type(e) == 10 and 94 in qualifier_ids(e)),
        "type32_all": count(lambda e: event_type(e) == 32),
        "type49_all": count(lambda e: event_type(e) == 49),
        "type49_outcome1": count(lambda e: event_type(e) == 49 and event_outcome(e) == 1),
        "type49_outcome0": count(lambda e: event_type(e) == 49 and event_outcome(e) == 0),
        "type52_keeper_pickup": count(lambda e: event_type(e) == 52),
        "type54_keeper_smother": count(lambda e: event_type(e) == 54),
        "type59_all": count(lambda e: event_type(e) == 59),
        "type61_all": count(lambda e: event_type(e) == 61),
        "type74_blocked_pass": count(lambda e: event_type(e) == 74),
        "q94_any_type": count(lambda e: 94 in qualifier_ids(e)),
        "q82_any_type": count(lambda e: 82 in qualifier_ids(e)),
    }


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    scalars = {
        k: v for k, v in event.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }
    return {
        "event_id": event.get("eventId", event.get("id")),
        "type_id": event_type(event),
        "outcome": event_outcome(event),
        "player_id": event_player_id(event),
        "player_name": event.get("playerName"),
        "period": event.get("periodId"),
        "minute": event.get("timeMin"),
        "second": event.get("timeSec"),
        "x": event.get("x"),
        "y": event.get("y"),
        "key_pass": is_key_pass(event),
        "qualifiers": [
            {
                "id": q.get("qualifierId", q.get("id", q.get("typeId"))),
                "value": q.get("value", q.get("qualifierValue")),
            }
            for q in qualifiers(event)
        ],
        "raw_scalar_fields": scalars,
    }


def main() -> None:
    transformed = load_json(TRANSFORMED_PATH, {})
    fixtures = load_json(FIXTURES_PATH, [])
    raw_player_feed = load_json(RAW_PLAYERS_PATH, {})

    players = transformed.get("players", []) if isinstance(transformed, dict) else []
    raw_players = value_of(raw_player_feed) or []

    if not players:
        raise SystemExit("ERROR: No players found in transformed_data.json")
    if not raw_players:
        raise SystemExit(
            "ERROR: No raw player feed found at raw_feeds/players.json. Run get_data.py first."
        )

    # Exact production Opta -> transformed-player mapping.
    opta_to_player: dict[str, dict[str, Any]] = {}
    for player in players:
        opta_id = compact_opta_id(player.get("Opta Player ID"))
        if opta_id:
            opta_to_player[opta_id] = player

    completed_matches = build_completed_match_map(raw_players, fixtures)
    if not completed_matches:
        raise SystemExit("ERROR: No completed matches discovered from raw player match arrays.")

    # Match every screenshot control to transformed_data before touching events.
    target_by_opta_id: dict[str, dict[str, Any]] = {}
    unmatched_controls = []

    for gt in GROUND_TRUTH:
        player = match_ground_truth_player(gt, players)
        if player is None:
            unmatched_controls.append(gt["name"])
            continue
        opta_id = compact_opta_id(player.get("Opta Player ID"))
        if not opta_id:
            unmatched_controls.append(gt["name"] + " [missing Opta Player ID]")
            continue
        target_by_opta_id[opta_id] = {
            "gt": gt,
            "player": player,
            "events": [],
            "match_ids": [],
        }

    if unmatched_controls:
        raise SystemExit(
            "ERROR: Screenshot controls failed player mapping: " + ", ".join(unmatched_controls)
        )

    loaded_matches = []
    fetch_failures = []

    for match in completed_matches:
        opta_match_id = match["opta_match_id"]
        print(f"Loading Opta match {opta_match_id} ...")
        try:
            payload, source = fetch_match_events(opta_match_id)
            events = live_events(payload)
            if not events:
                raise ValueError("No liveData.event rows found")

            loaded_matches.append({
                **match,
                **match_metadata(payload),
                "source": source,
                "event_count": len(events),
            })

            # Assign only target player events, preserving match IDs.
            hit_ids = set()
            for event in events:
                pid = event_player_id(event)
                if pid in target_by_opta_id:
                    target_by_opta_id[pid]["events"].append(event)
                    hit_ids.add(pid)
            for pid in hit_ids:
                target_by_opta_id[pid]["match_ids"].append(opta_match_id)

            print(f"  {len(events)} events; target players hit: {len(hit_ids)}")

        except Exception as exc:
            fetch_failures.append({
                "opta_match_id": opta_match_id,
                "error": repr(exc),
            })
            print(f"  FAILED: {exc}")

    if not loaded_matches:
        raise SystemExit(
            "ERROR: No Opta match feeds loaded. Failures: "
            + json.dumps(fetch_failures, ensure_ascii=False)
        )

    output_players = []

    for opta_id, item in target_by_opta_id.items():
        gt = item["gt"]
        p = item["player"]
        events = item["events"]

        prod = current_production_counts(events)
        revised_att = revised_att_hypothesis(events)
        alts = alternative_counts(events)

        type_counts = Counter(event_type(e) for e in events if event_type(e) is not None)
        q_by_type: dict[str, Counter] = defaultdict(Counter)
        for e in events:
            typ = event_type(e)
            if typ is None:
                continue
            for qid in qualifier_ids(e):
                q_by_type[str(typ)][str(qid)] += 1

        output_players.append({
            "player": gt["name"],
            "matched_name": p.get("Name"),
            "club": p.get("Club") or p.get("Club Name"),
            "position": p.get("Position"),
            "opta_player_id": opta_id,
            "match_ids": item["match_ids"],
            "event_count": len(events),
            "official_wsl_ui": {
                "attacking_actions": gt["att"],
                "defensive_actions": gt["def"],
                "attacking_points": gt["att"] // 4,
                "defensive_points": gt["def"] // 10,
                "involvement_points": (gt["att"] // 4) + (gt["def"] // 10),
            },
            "current_production_parser": {
                **prod,
                "att_delta_vs_wsl": prod["attacking_actions"] - gt["att"],
                "def_delta_vs_wsl": prod["defensive_actions"] - gt["def"],
            },
            "revised_att_hypothesis": {
                **revised_att,
                "delta_vs_wsl": revised_att["attacking_actions"] - gt["att"],
            },
            "alternative_counts": alts,
            "all_event_type_counts": {
                str(k): v for k, v in sorted(type_counts.items())
            },
            "qualifier_counts_by_event_type": {
                typ: dict(sorted(counter.items(), key=lambda kv: int(kv[0])))
                for typ, counter in sorted(q_by_type.items(), key=lambda kv: int(kv[0]))
            },
            "events": [compact_event(e) for e in events],
        })

    zero_event_players = [r["player"] for r in output_players if r["event_count"] == 0]
    if zero_event_players:
        raise SystemExit(
            "ERROR: These screenshot controls matched transformed_data.json but had ZERO Opta "
            "events: " + ", ".join(zero_event_players)
        )

    output_players.sort(key=lambda r: (r["club"] or "", r["player"]))

    exact_prod_att = sum(
        r["current_production_parser"]["att_delta_vs_wsl"] == 0
        for r in output_players
    )
    exact_revised_att = sum(
        r["revised_att_hypothesis"]["delta_vs_wsl"] == 0
        for r in output_players
    )
    exact_prod_def = sum(
        r["current_production_parser"]["def_delta_vs_wsl"] == 0
        for r in output_players
    )

    payload = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": "Exact production-parser reconciliation against official WSL Fantasy screenshot controls.",
            "production_changed": False,
            "source": "Public Opta Player Stats widget match-event feed",
            "widget_feed_id": WIDGET_FEED_ID,
            "ground_truth_player_count": len(GROUND_TRUTH),
            "players_audited": len(output_players),
            "players_with_zero_events": zero_event_players,
            "loaded_matches": loaded_matches,
            "match_fetch_failures": fetch_failures,
            "exact_current_att": exact_prod_att,
            "exact_revised_att_hypothesis": exact_revised_att,
            "exact_current_def": exact_prod_def,
            "notes": [
                "Current production parser is reproduced exactly from fetch_involvement_points.py supplied 2026-09-05.",
                "revised_att_hypothesis is diagnostic only and does not modify production.",
                "Do not force defensive definitions to match WSL until candidate rules survive all controls.",
            ],
        },
        "ground_truth": GROUND_TRUTH,
        "players": output_players,
    }

    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nWrote {OUTPUT_PATH.name}")
    print(
        f"Players: {len(output_players)} | "
        f"Current ATT exact: {exact_prod_att}/{len(output_players)} | "
        f"Revised ATT exact: {exact_revised_att}/{len(output_players)} | "
        f"Current DEF exact: {exact_prod_def}/{len(output_players)}"
    )
    print("\nPer-player reconciliation:")
    for r in output_players:
        print(
            f'{r["player"]}: '
            f'ATT current {r["current_production_parser"]["attacking_actions"]}/'
            f'{r["official_wsl_ui"]["attacking_actions"]}, '
            f'ATT revised {r["revised_att_hypothesis"]["attacking_actions"]}/'
            f'{r["official_wsl_ui"]["attacking_actions"]}, '
            f'DEF {r["current_production_parser"]["defensive_actions"]}/'
            f'{r["official_wsl_ui"]["defensive_actions"]}'
        )


if __name__ == "__main__":
    main()
