#!/usr/bin/env python3
"""
Targeted involvement audit for tackle-related discrepancies.

Purpose
-------
Inspect the raw Opta/Stats Perform events behind the involvement table, with an
initial focus on Jade Richards and Lucy Bronze.

The audit answers:
  1. How many Opta type-7 (Tackle) events does each target have?
  2. How many have outcome=1 vs outcome=0?
  3. What would the player's defensive-action total be if fantasy counts:
       a) only successful type-7 tackles (current parser), or
       b) every type-7 tackle?
  4. What are the exact raw defensive/tackle-related events and qualifiers?
  5. Are there other event types for the target players that look plausibly
     tackle/challenge-related and deserve inspection?

Inputs:
  - transformed_data.json
  - involvement_history.json
  - raw_opta_events/*.json (preferred cache)
If a raw match payload is missing from cache, the script fetches the same public
Stats Perform widget feed used by the involvement parser.

Output:
  - tackle_audit.json
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
HISTORY_PATH = ROOT / "involvement_history.json"
LIVE_PATH = ROOT / "involvement_live.json"
RAW_EVENT_DIR = ROOT / "raw_opta_events"
OUTPUT_PATH = ROOT / "tackle_audit.json"

DEFAULT_WIDGET_FEED_ID = "ft1tiv1inq7v1sk3y9tv12yh5"
WIDGET_FEED_ID = os.getenv("OPTA_WIDGET_FEED_ID", DEFAULT_WIDGET_FEED_ID)
PERFORMFEEDS_BASE = "https://api.performfeeds.com/soccerdata/matchevent"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 WSL fantasy involvement tackle audit",
    "Accept": "*/*",
    "Referer": "https://optaplayerstats.statsperform.com/",
}

# Same event definitions used by the production involvement parser.
TACKLE = 7
INTERCEPTION = 8
DEFENSIVE_SAVE_OR_BLOCK = 10
CLEARANCE = 12
BALL_RECOVERY = 49
DEF_BLOCK_QUALIFIER = 94
CROSS_QUALIFIER = 2
PASS = 1
TAKE_ON = 3
SHOT_SAVED = 15
GOAL = 16
OWN_GOAL_QUALIFIER = 28

TARGET_NAMES = {
    "jade richards",
    "lucy bronze",
    "maria pilar leon cebrian",
    "danielle van de donk",
    "alexia putellas",
    "claudia mummery-walker",
    "hannah hampton",
    "katie mccabe",
    "alyssa thompson",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def fetch_match_events(opta_match_id: str) -> dict[str, Any]:
    RAW_EVENT_DIR.mkdir(exist_ok=True)
    cache_path = RAW_EVENT_DIR / f"{opta_match_id}.json"
    if cache_path.exists():
        return load_json(cache_path, {})

    params = {
        "_rt": "c",
        "_lcl": "en",
        "_fmt": "jsonp",
        "sps": "widgets",
        "_clbk": "wslFantasyTackleAudit",
    }
    url = f"{PERFORMFEEDS_BASE}/{WIDGET_FEED_ID}/{opta_match_id}"
    response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=45)
    response.raise_for_status()
    payload = strip_jsonp(response.text)
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def live_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    live = payload.get("liveData") or {}
    events = live.get("event") or []
    return events if isinstance(events, list) else []


def event_player_id(event: dict[str, Any]) -> str:
    for key in ("playerId", "participantId", "player_id", "participant_id"):
        if event.get(key):
            return compact_opta_id(event.get(key))
    return ""


def event_type(event: dict[str, Any]) -> int | None:
    try:
        return int(event.get("typeId"))
    except (TypeError, ValueError):
        return None


def event_outcome(event: dict[str, Any]) -> int:
    try:
        return int(event.get("outcome"))
    except (TypeError, ValueError):
        return 0


def qualifier_rows(event: dict[str, Any]) -> list[dict[str, Any]]:
    q = event.get("qualifier") or event.get("qualifiers") or []
    return q if isinstance(q, list) else []


def qualifier_ids(event: dict[str, Any]) -> list[int]:
    out = []
    for q in qualifier_rows(event):
        try:
            out.append(int(q.get("qualifierId")))
        except (TypeError, ValueError, AttributeError):
            pass
    return out


def minute_label(event: dict[str, Any]) -> str:
    minute = event.get("timeMin")
    second = event.get("timeSec")
    period = event.get("periodId")
    parts = []
    if minute is not None:
        parts.append(str(minute))
    if second is not None:
        parts.append(f"{second}s")
    if period is not None:
        parts.append(f"P{period}")
    return " ".join(parts)


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep enough raw detail to diagnose semantics without dumping 1,800 events."""
    return {
        "event_id": event.get("eventId") or event.get("id"),
        "type_id": event_type(event),
        "outcome": event_outcome(event),
        "minute": event.get("timeMin"),
        "second": event.get("timeSec"),
        "period_id": event.get("periodId"),
        "player_id": event_player_id(event),
        "team_id": compact_opta_id(event.get("contestantId") or event.get("teamId")),
        "qualifier_ids": qualifier_ids(event),
        "qualifiers": qualifier_rows(event),
        # Preserve useful descriptive fields when present in this feed.
        "description": event.get("description"),
        "event_name": event.get("typeName") or event.get("eventTypeName"),
        "x": event.get("x"),
        "y": event.get("y"),
    }


def defensive_counts(events: list[dict[str, Any]], pid: str) -> dict[str, int]:
    counts = Counter()
    for event in events:
        if event_player_id(event) != pid:
            continue
        typ = event_type(event)
        outcome = event_outcome(event)
        qids = set(qualifier_ids(event))

        if typ == TACKLE:
            counts["type7_all"] += 1
            if outcome == 1:
                counts["tackles_current"] += 1
            else:
                counts["type7_unsuccessful"] += 1
        if typ == INTERCEPTION:
            counts["interceptions"] += 1
        if typ == CLEARANCE:
            counts["clearances"] += 1
        if typ == DEFENSIVE_SAVE_OR_BLOCK and DEF_BLOCK_QUALIFIER in qids:
            counts["blocks"] += 1
        if typ == BALL_RECOVERY:
            counts["recoveries"] += 1

    current = (
        counts["tackles_current"]
        + counts["interceptions"]
        + counts["clearances"]
        + counts["blocks"]
        + counts["recoveries"]
    )
    all_type7 = (
        counts["type7_all"]
        + counts["interceptions"]
        + counts["clearances"]
        + counts["blocks"]
        + counts["recoveries"]
    )
    counts["def_actions_current"] = current
    counts["def_points_current"] = current // 10
    counts["def_actions_if_all_type7"] = all_type7
    counts["def_points_if_all_type7"] = all_type7 // 10
    return dict(counts)


def attacking_counts(events: list[dict[str, Any]], pid: str) -> dict[str, int]:
    counts = Counter()
    for event in events:
        if event_player_id(event) != pid:
            continue
        typ = event_type(event)
        outcome = event_outcome(event)
        qids = set(qualifier_ids(event))

        # Same attacking definitions as the involvement parser.
        if typ == SHOT_SAVED:
            counts["shots_on_target"] += 1
        if typ == GOAL and OWN_GOAL_QUALIFIER not in qids:
            counts["shots_on_target"] += 1
        if typ == PASS:
            if event.get("keypass") or event.get("keyPass"):
                counts["key_passes"] += 1
            if outcome == 1 and CROSS_QUALIFIER in qids:
                counts["successful_crosses"] += 1
        if typ == TAKE_ON and outcome == 1:
            counts["successful_dribbles"] += 1

    total = (
        counts["shots_on_target"]
        + counts["key_passes"]
        + counts["successful_crosses"]
        + counts["successful_dribbles"]
    )
    counts["attacking_actions"] = total
    counts["attacking_points"] = total // 4
    return dict(counts)


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = (
        text.replace("í", "i")
            .replace("é", "e")
            .replace("è", "e")
            .replace("ë", "e")
            .replace("á", "a")
            .replace("à", "a")
            .replace("ä", "a")
            .replace("ó", "o")
            .replace("ö", "o")
            .replace("ú", "u")
            .replace("ü", "u")
            .replace("ñ", "n")
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


TARGET_NORMALIZED = {normalize_name(name) for name in TARGET_NAMES}

# Common display/feed variants seen in the fantasy data.
TARGET_ALIASES = {
    "mapi leon": "maria pilar leon cebrian",
    "maria pilar leon": "maria pilar leon cebrian",
    "maria pilar leon cebrian": "maria pilar leon cebrian",
    "claudia walker": "claudia mummery-walker",
    "claudia mummery walker": "claudia mummery-walker",
    "claudia mummery-walker": "claudia mummery-walker",
}


def find_targets(transformed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = {}
    for player in transformed.get("players", []) or []:
        name = str(player.get("Name") or "").strip()
        norm = normalize_name(name)
        canonical = TARGET_ALIASES.get(norm, norm)
        if canonical in TARGET_NORMALIZED:
            pid = compact_opta_id(player.get("Opta Player ID"))
            if pid:
                targets[pid] = {
                    "name": name,
                    "club": player.get("Club"),
                    "position": player.get("Position"),
                    "opta_player_id": pid,
                }
    return targets


def main() -> None:
    transformed = load_json(TRANSFORMED_PATH, {})
    history = load_json(HISTORY_PATH, {})
    targets = find_targets(transformed)

    if not targets:
        raise SystemExit("Could not find Richards/Bronze Opta player IDs in transformed_data.json")

    output = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "purpose": "Audit involvement-point event definitions using settled + latest matches for user-scored players plus Richards/Bronze controls",
            "current_rule": "typeId 7 AND outcome=1 counts as a tackle won",
            "comparison_rule": "all typeId 7 events count as tackles",
            "targets": list(targets.values()),
        },
        "players": [],
    }

    live = load_json(LIVE_PATH, {})

    # Audit the union of settled history and latest snapshot, preferring settled
    # metadata if the same Opta match appears in both.
    combined_matches = {}
    for match in live.get("matches", []) or []:
        opta_match_id = str(match.get("opta_match_id") or "")
        if opta_match_id:
            combined_matches[opta_match_id] = match
    for match in history.get("matches", []) or []:
        opta_match_id = str(match.get("opta_match_id") or "")
        if opta_match_id:
            combined_matches[opta_match_id] = match

    for opta_match_id, match in combined_matches.items():
        payload = fetch_match_events(opta_match_id)
        events = live_events(payload)
        player_ids_in_match = {event_player_id(e) for e in events}

        for pid, target in targets.items():
            if pid not in player_ids_in_match:
                continue

            all_player_events = [e for e in events if event_player_id(e) == pid]
            type_counts = Counter(event_type(e) for e in all_player_events if event_type(e) is not None)
            type7 = [e for e in all_player_events if event_type(e) == TACKLE]

            # Include all current defensive categories plus every type-7 event verbatim-ish.
            defensive_raw = [
                e for e in all_player_events
                if event_type(e) in {TACKLE, INTERCEPTION, DEFENSIVE_SAVE_OR_BLOCK, CLEARANCE, BALL_RECOVERY}
            ]

            # Also provide all event-type counts for the target so a suspicious adjacent
            # event type can be spotted without another code change.
            row = {
                **target,
                "match": {
                    "opta_match_id": opta_match_id,
                    "description": match.get("description"),
                    "date": match.get("date"),
                    "fantasy_game_week": match.get("fantasy_game_week"),
                },
                "attacking_counts": attacking_counts(events, pid),
                "counts": defensive_counts(events, pid),
                "all_event_type_counts": {str(k): v for k, v in sorted(type_counts.items())},
                "type7_tackle_events": [compact_event(e) for e in type7],
                "defensive_related_events": [compact_event(e) for e in defensive_raw],
            }
            output["players"].append(row)

    # Add a quick human-readable conclusion block.
    conclusions = []
    for row in output["players"]:
        c = row["counts"]
        a = row.get("attacking_counts", {})
        conclusions.append({
            "player": row["name"],
            "match": row["match"]["description"],
            "shots_on_target": a.get("shots_on_target", 0),
            "key_passes": a.get("key_passes", 0),
            "successful_crosses": a.get("successful_crosses", 0),
            "successful_dribbles": a.get("successful_dribbles", 0),
            "attacking_actions": a.get("attacking_actions", 0),
            "attacking_points": a.get("attacking_points", 0),
            "successful_type7_tackles": c.get("tackles_current", 0),
            "all_type7_tackles": c.get("type7_all", 0),
            "unsuccessful_type7_tackles": c.get("type7_unsuccessful", 0),
            "current_def_actions": c.get("def_actions_current", 0),
            "current_def_points": c.get("def_points_current", 0),
            "def_actions_if_all_type7_count": c.get("def_actions_if_all_type7", 0),
            "def_points_if_all_type7_count": c.get("def_points_if_all_type7", 0),
            "current_total_involvement_points": a.get("attacking_points", 0) + c.get("def_points_current", 0),
            "total_involvement_points_if_all_type7_count": a.get("attacking_points", 0) + c.get("def_points_if_all_type7", 0),
        })
    output["quick_comparison"] = conclusions

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {OUTPUT_PATH.name}")
    for row in conclusions:
        print(
            f"{row['player']}: att {row['attacking_actions']} => {row['attacking_points']} IP; "
            f"def current {row['current_def_actions']} => {row['current_def_points']} IP "
            f"({row['successful_type7_tackles']} won / {row['all_type7_tackles']} all type-7); "
            f"def all-type7 {row['def_actions_if_all_type7_count']} => {row['def_points_if_all_type7_count']} IP; "
            f"combined {row['current_total_involvement_points']} vs {row['total_involvement_points_if_all_type7_count']}"
        )


if __name__ == "__main__":
    main()
