#!/usr/bin/env python3
"""
Fetch public Opta/Stats Perform match-event data used by the Opta Player Stats
widget and aggregate the actions used by WSL Fantasy "Involvement Points".

PRODUCTION VERSION
-----------------------
Discovers past fixtures, confirms completion from Opta, caches only completed-match
Opta event data, calculates the WSL Fantasy attacking/defensive involvement
actions match-by-match, and writes a compact history file for the Involvement
Points frontend tab.

Inputs expected in the repo:
  - transformed_data.json
  - fixtures.json
  - raw_feeds/players.json   (created by get_data.py)

Outputs:
  - involvement_history.json
  - raw_opta_events/<opta_match_id>.json

The public widget feed identifier can be overridden with OPTA_WIDGET_FEED_ID.
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
OUTPUT_PATH = ROOT / "involvement_history.json"
RAW_EVENT_DIR = ROOT / "raw_opta_events"

# Public widget feed id observed on the Opta Player Stats page on 2026-09-05.
# Keep overrideable in case Stats Perform rotates the public widget identifier.
DEFAULT_WIDGET_FEED_ID = "ft1tiv1inq7v1sk3y9tv12yh5"
WIDGET_FEED_ID = os.getenv("OPTA_WIDGET_FEED_ID", DEFAULT_WIDGET_FEED_ID)

PERFORMFEEDS_BASE = "https://api.performfeeds.com/soccerdata/matchevent"

# Opta F24 event ids used by the fantasy action definitions.
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
KEEPER_CLAIM = 11
KEEPER_PUNCH = 41
KEEPER_PICKUP = 52

# Qualifiers.
CROSS_QUALIFIER = 2
OWN_GOAL_QUALIFIER = 28
SHOT_BLOCKED_QUALIFIER = 82
DEF_BLOCK_QUALIFIER = 94

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 WSL fantasy involvement stats parser",
    "Accept": "*/*",
    "Referer": "https://optaplayerstats.statsperform.com/",
}


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
    """Parse either raw JSON or callbackName(<json>)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", text, flags=re.S)
    if not match:
        raise ValueError("Response was neither JSON nor recognizable JSONP.")
    return json.loads(match.group(1))


def payload_match_status(payload: dict[str, Any]) -> str:
    live = payload.get("liveData") or {}
    details = live.get("matchDetails") or {}
    return str(details.get("matchStatus") or "").strip()


def payload_is_played(payload: dict[str, Any]) -> bool:
    return payload_match_status(payload).lower() == "played"


def fetch_match_events(opta_match_id: str, force: bool = False) -> dict[str, Any]:
    """Fetch one Opta event payload.

    Completed payloads are cached permanently. A cached non-completed payload is
    never trusted as final: it is re-fetched so an in-progress snapshot cannot
    freeze a match in history forever. Newly fetched non-completed payloads are
    returned to the caller but are not written to raw_opta_events/.
    """
    RAW_EVENT_DIR.mkdir(exist_ok=True)
    cache_path = RAW_EVENT_DIR / f"{opta_match_id}.json"

    if cache_path.exists() and not force:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload_is_played(cached):
            return cached
        print(f"  Cached payload for {opta_match_id} is not final; refreshing it.")

    # The public widget uses JSONP. Callback value is arbitrary but required by
    # the endpoint shape observed in Firefox.
    params = {
        "_rt": "c",
        "_lcl": "en",
        "_fmt": "jsonp",
        "sps": "widgets",
        "_clbk": "wslFantasyInvolvement",
    }
    url = f"{PERFORMFEEDS_BASE}/{WIDGET_FEED_ID}/{opta_match_id}"
    response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=45)
    response.raise_for_status()
    payload = strip_jsonp(response.text)

    # History should only persist final match payloads. Live/provisional snapshots
    # belong in involvement_live.json and must not become permanent raw history.
    if payload_is_played(payload):
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return payload

def qualifiers(event: dict[str, Any]) -> list[dict[str, Any]]:
    q = event.get("qualifier") or event.get("qualifiers") or []
    return q if isinstance(q, list) else []


def qualifier_ids(event: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for q in qualifiers(event):
        raw = q.get("qualifierId") if isinstance(q, dict) else None
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def event_player_id(event: dict[str, Any]) -> str:
    """Handle common Perform/Opta names without assuming one schema spelling."""
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
    # Observed structure: payload.liveData.event
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


def aggregate_match(
    payload: dict[str, Any],
    opta_to_player: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events = live_events(payload)

    per_player: dict[str, Counter] = defaultdict(Counter)
    event_type_counts = Counter()
    unmatched_player_events = Counter()
    recovery_32_count = 0

    for event in events:
        typ = event_type(event)
        if typ is None:
            continue

        event_type_counts[typ] += 1
        pid = event_player_id(event)
        if not pid:
            continue

        qids = qualifier_ids(event)
        outcome = event_outcome(event)

        if pid not in opta_to_player:
            unmatched_player_events[pid] += 1

        row = per_player[pid]

        # Attacking involvement
        if typ == ATTEMPT_SAVED and SHOT_BLOCKED_QUALIFIER not in qids:
            row["shots_on_target"] += 1
        elif typ == GOAL and OWN_GOAL_QUALIFIER not in qids:
            row["shots_on_target"] += 1

        if is_key_pass(event):
            row["key_passes"] += 1

        if typ == PASS and outcome == 1 and CROSS_QUALIFIER in qids:
            row["successful_crosses"] += 1

        if typ == TAKE_ON and outcome == 1:
            row["successful_dribbles"] += 1

        # Defensive involvement
        if typ == TACKLE and outcome == 1:
            row["tackles_won"] += 1

        if typ == INTERCEPTION:
            row["interceptions"] += 1

        if typ == CLEARANCE:
            row["clearances"] += 1

        # Opta defensive block: event 10 with qualifier 94 ("Def block").
        if typ == DEFENSIVE_SAVE_OR_BLOCK and DEF_BLOCK_QUALIFIER in qids:
            row["blocks"] += 1

        if typ == BALL_RECOVERY_COMMON:
            row["recoveries"] += 1

        # Goalkeeper defensive actions confirmed by MW1 reconciliation:
        # Type 11 claim, Type 41 punch, Type 52 pickup.
        # Type 54 smother and Type 59 are deliberately NOT counted.
        player_meta = opta_to_player.get(pid, {})
        if str(player_meta.get("Position") or "").upper() == "GK":
            if typ == KEEPER_CLAIM:
                row["keeper_claims"] += 1
            if typ == KEEPER_PUNCH:
                row["keeper_punches"] += 1
            if typ == KEEPER_PICKUP:
                row["keeper_pickups"] += 1

        # Keep alternative id 32 visible as a diagnostic rather than silently
        # counting it until we validate this competition's feed semantics.
        if typ == BALL_RECOVERY_ALT:
            recovery_32_count += 1
            row["recovery_type32_diagnostic"] += 1

    results: list[dict[str, Any]] = []
    all_player_ids = set(per_player)

    for pid in sorted(all_player_ids):
        counts = per_player[pid]
        player = opta_to_player.get(pid, {})
        attacking_actions = (
            counts["shots_on_target"]
            + counts["key_passes"]
            + counts["successful_crosses"]
            + counts["successful_dribbles"]
        )
        defensive_actions = (
            counts["tackles_won"]
            + counts["interceptions"]
            + counts["clearances"]
            + counts["blocks"]
            + counts["recoveries"]
            + counts["keeper_claims"]
            + counts["keeper_punches"]
            + counts["keeper_pickups"]
        )

        results.append({
            "opta_player_id": pid,
            "player_id": player.get("Player ID"),
            "name": player.get("Name") or f"Unmatched Opta player {pid}",
            "club": player.get("Club"),
            "league": player.get("League"),
            "position": player.get("Position"),
            "shots_on_target": counts["shots_on_target"],
            "key_passes": counts["key_passes"],
            "successful_crosses": counts["successful_crosses"],
            "successful_dribbles": counts["successful_dribbles"],
            "attacking_actions": attacking_actions,
            "attacking_points": attacking_actions // 4,
            "tackles_won": counts["tackles_won"],
            "interceptions": counts["interceptions"],
            "clearances": counts["clearances"],
            "blocks": counts["blocks"],
            "recoveries": counts["recoveries"],
            "keeper_claims": counts["keeper_claims"],
            "keeper_punches": counts["keeper_punches"],
            "keeper_pickups": counts["keeper_pickups"],
            "defensive_actions": defensive_actions,
            "defensive_points": defensive_actions // 10,
            "involvement_points": (attacking_actions // 4) + (defensive_actions // 10),
            "recovery_type32_diagnostic": counts["recovery_type32_diagnostic"],
        })

    diagnostics = {
        "event_count": len(events),
        "event_type_counts": {str(k): v for k, v in sorted(event_type_counts.items())},
        "unmatched_opta_player_ids": dict(unmatched_player_events.most_common()),
        "type32_recovery_like_events": recovery_32_count,
        "mapping_notes": {
            "shots_on_target": "Type 15 excluding qualifier 82 + non-own-goal type 16.",
            "key_passes": "Any event carrying keypass/keyPass/key_pass flag.",
            "successful_crosses": "Successful pass (outcome=1) with qualifier 2 (Cross).",
            "successful_dribbles": "Successful Take On (type 3, outcome=1).",
            "tackles_won": "Tackle (type 7, outcome=1).",
            "interceptions": "Interception (type 8).",
            "clearances": "Clearance (type 12).",
            "blocks": "Event 10 with qualifier 94 (Def block).",
            "recoveries": "Type 49 counted; type 32 retained separately for validation.",
            "goalkeeper_actions": "For GK only: type 11 claims + type 41 punches + type 52 pickups. Types 54/59 excluded.",
        },
    }
    return results, diagnostics


def parse_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def completed_match_ids_from_player_feed(
    raw_players: list[dict[str, Any]],
) -> set[str]:
    """Return WSL match ids explicitly marked completed by the player feed.

    The WSL feed can lag, so this is a useful confirmation signal but no longer
    the sole gate for discovering finished matches.
    """
    completed: set[str] = set()

    for player in raw_players:
        for match in player.get("matches", []) or []:
            match_id = str(match.get("matchId") or "")
            if not match_id:
                continue

            try:
                if int(match.get("matchdayStatus")) == 5:
                    completed.add(match_id)
            except (TypeError, ValueError):
                continue

    return completed


def build_past_match_candidates(
    raw_players: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build candidate historical matches from fixtures.json.

    fixtures.json is the canonical source for match ids, Opta provider ids,
    kickoff times and fantasy gameweeks. Its own status fields are deliberately
    ignored because they have remained UPCOMING/Fixture even after matches were
    completed.

    Any fixture whose kickoff has passed is a candidate. The caller then asks
    Opta whether the match is actually Played before adding it to permanent
    involvement history.
    """
    now = datetime.now(timezone.utc)
    wsl_completed_ids = completed_match_ids_from_player_feed(raw_players)
    out: list[dict[str, Any]] = []

    for fixture in fixtures:
        match_id = str(fixture.get("match_id") or "")
        provider_id = fixture.get("provider_id")
        opta_match_id = compact_opta_id(provider_id)
        kickoff = parse_utc_datetime(
            fixture.get("match_date_time_utc") or fixture.get("deadline_date")
        )

        if not match_id or not opta_match_id or kickoff is None:
            continue
        if kickoff > now:
            continue

        out.append({
            "match_id": match_id,
            "date": fixture.get("match_date_time_utc") or fixture.get("deadline_date"),
            "matchday_id": fixture.get("gameday_id"),
            "opta_match_id": opta_match_id,
            "home_id": fixture.get("home_id"),
            "away_id": fixture.get("away_id"),
            "competition_id": fixture.get("competition_id"),
            "league_game_week": fixture.get("league_game_week") or fixture.get("game_week"),
            "fantasy_game_week": fixture.get("fantasy_game_week"),
            "wsl_player_feed_completed": match_id in wsl_completed_ids,
        })

    return sorted(out, key=lambda r: (str(r.get("date") or ""), r["opta_match_id"]))

def build_overall_rows(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate exact match-level involvement totals without re-applying thresholds."""
    by_player: dict[str, dict[str, Any]] = {}

    count_fields = (
        "shots_on_target", "key_passes", "successful_crosses", "successful_dribbles",
        "attacking_actions", "attacking_points", "tackles_won", "interceptions",
        "clearances", "blocks", "recoveries", "keeper_claims", "keeper_punches",
        "keeper_pickups", "defensive_actions", "defensive_points", "involvement_points",
    )

    for match in matches:
        for row in match.get("players", []) or []:
            pid = str(row.get("player_id") or row.get("opta_player_id") or row.get("name"))
            if pid not in by_player:
                by_player[pid] = {
                    "player_id": row.get("player_id"),
                    "opta_player_id": row.get("opta_player_id"),
                    "name": row.get("name"),
                    "club": row.get("club"),
                    "league": row.get("league"),
                    "position": row.get("position"),
                    "matches_with_data": 0,
                    **{field: 0 for field in count_fields},
                }

            agg = by_player[pid]
            agg["matches_with_data"] += 1
            for field in count_fields:
                agg[field] += int(row.get(field) or 0)

    return sorted(
        by_player.values(),
        key=lambda r: (-int(r.get("involvement_points") or 0),
                       -int(r.get("attacking_actions") or 0),
                       -int(r.get("defensive_actions") or 0),
                       str(r.get("name") or "")),
    )



def main() -> None:
    transformed = load_json(TRANSFORMED_PATH, {})
    fixtures = load_json(FIXTURES_PATH, [])
    raw_player_feed = load_json(RAW_PLAYERS_PATH, {})

    players = transformed.get("players", []) if isinstance(transformed, dict) else []
    raw_players = value_of(raw_player_feed) or []

    if not players:
        raise SystemExit("No players found in transformed_data.json")
    if not raw_players:
        raise SystemExit(
            "No raw player feed found at raw_feeds/players.json. Run get_data.py first."
        )

    opta_to_player = {}
    for player in players:
        opta_id = compact_opta_id(player.get("Opta Player ID"))
        if opta_id:
            opta_to_player[opta_id] = player

    match_candidates = build_past_match_candidates(raw_players, fixtures)
    if not match_candidates:
        raise SystemExit("No past fixture candidates with Opta provider ids were found.")

    output = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "Public Opta Player Stats widget match-event feed",
            "widget_feed_id": WIDGET_FEED_ID,
            "status": "production",
            "note": (
                "Involvement thresholds are applied per match, then summed. "
                "Raw event payloads are cached by Opta match id. "
                "Values are rules/Opta-derived; known WSL UI discrepancies are not force-fitted."
            ),
        },
        "matches": [],
    }

    failures = []

    for match in match_candidates:
        opta_match_id = match["opta_match_id"]
        source_note = (
            "WSL-completed + Opta check"
            if match.get("wsl_player_feed_completed")
            else "past fixture + Opta fallback"
        )
        print(f"Checking {opta_match_id} ({source_note}) ...")
        try:
            payload = fetch_match_events(opta_match_id)
            status = payload_match_status(payload)

            if not payload_is_played(payload):
                print(f"  Skipping: Opta matchStatus={status or 'unknown'}")
                continue

            player_rows, diagnostics = aggregate_match(payload, opta_to_player)
            output["matches"].append({
                **match,
                **match_metadata(payload),
                "players": player_rows,
                "diagnostics": diagnostics,
            })
            print(
                f"  Played: {diagnostics['event_count']} events, "
                f"{len(player_rows)} player rows"
            )
        except Exception as exc:
            failures.append({"opta_match_id": opta_match_id, "error": str(exc)})
            print(f"  FAILED: {exc}")

    output["metadata"]["failures"] = failures
    output["metadata"]["available_gameweeks"] = sorted(
        {
            str(m.get("fantasy_game_week"))
            for m in output["matches"]
            if m.get("fantasy_game_week") not in (None, "")
        },
        key=lambda x: int(x),
    )
    output["metadata"]["match_count"] = len(output["matches"])
    output["players_overall"] = build_overall_rows(output["matches"])

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {OUTPUT_PATH.name}")
    if failures:
        print(f"WARNING: {len(failures)} match fetch(es) failed.")


if __name__ == "__main__":
    main()
