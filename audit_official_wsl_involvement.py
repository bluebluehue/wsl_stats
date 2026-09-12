#!/usr/bin/env python3
"""
Audit our Opta-derived WSL Fantasy involvement totals against the official
WSL Fantasy player-detail JSON feed.

INPUT
-----
- involvement_history.json

OUTPUT
------
- official_wsl_involvement_audit.json
- official_wsl_involvement_audit.csv
- raw_wsl_official_player_stats/<player-id>.json

This script DOES NOT change production involvement logic. It is diagnostic only.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "involvement_history.json"
OUTPUT_JSON = ROOT / "official_wsl_involvement_audit.json"
OUTPUT_CSV = ROOT / "official_wsl_involvement_audit.csv"
RAW_OFFICIAL_DIR = ROOT / "raw_wsl_official_player_stats"

BASE_URL = "https://gaming.wslfootball.com/feeds/popup/stats"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 WSL fantasy involvement reconciliation audit",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://gaming.wslfootball.com/",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def recursive_find_key(obj: Any, target_key: str) -> list[Any]:
    """Return all values found under target_key anywhere in a JSON structure."""
    found: list[Any] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == target_key:
                found.append(value)
            found.extend(recursive_find_key(value, target_key))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(recursive_find_key(value, target_key))

    return found


def recursive_find_candidate_stat_lists(payload: Any) -> list[list[dict[str, Any]]]:
    """
    Find lists that look like official per-match / matchday stat rows.
    Prefer explicit matchdayStats, then fall back to structural detection.
    """
    candidates: list[list[dict[str, Any]]] = []

    explicit = recursive_find_key(payload, "matchdayStats")
    for value in explicit:
        if isinstance(value, list) and all(isinstance(x, dict) for x in value):
            candidates.append(value)

    if candidates:
        return candidates

    def walk(obj: Any) -> None:
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            keys = set().union(*(x.keys() for x in obj))
            if (
                {"attackingActions", "defensiveActions"} & keys
                and {"matchdayId", "matchId"} & keys
            ):
                candidates.append(obj)
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(payload)
    return candidates


def fetch_official_player(player_id: str, force: bool = False) -> dict[str, Any]:
    RAW_OFFICIAL_DIR.mkdir(exist_ok=True)

    safe_name = player_id.replace("::", "__").replace(":", "_")
    cache_path = RAW_OFFICIAL_DIR / f"{safe_name}.json"

    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    encoded_id = quote(player_id, safe=":")
    url = f"{BASE_URL}/player_en_1_{encoded_id}.json"
    params = {
        "v": "3",
        "buster": str(int(time.time() * 1000)),
    }

    response = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def normalize_official_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_lists = recursive_find_candidate_stat_lists(payload)

    if not candidate_lists:
        return []

    # Use the richest candidate list.
    stats = max(candidate_lists, key=len)

    normalized: list[dict[str, Any]] = []
    for row in stats:
        if not isinstance(row, dict):
            continue

        normalized.append({
            "match_id": row.get("matchId") or row.get("match_id"),
            "matchday_id": safe_int(
                row.get("matchdayId")
                if "matchdayId" in row
                else row.get("matchday_id")
            ),
            "attacking_actions": safe_int(
                row.get("attackingActions")
                if "attackingActions" in row
                else row.get("attacking_actions")
            ),
            "attacking_points": safe_int(
                row.get("attackingPoints")
                if "attackingPoints" in row
                else row.get("attacking_points")
            ),
            "defensive_actions": safe_int(
                row.get("defensiveActions")
                if "defensiveActions" in row
                else row.get("defensive_actions")
            ),
            "defensive_points": safe_int(
                row.get("defensivePoints")
                if "defensivePoints" in row
                else row.get("defensive_points")
            ),
            "total_points": safe_int(
                row.get("totalPoints")
                if "totalPoints" in row
                else row.get("total_points")
            ),
            "raw": row,
        })

    return normalized


def match_official_row(
    official_rows: list[dict[str, Any]],
    match_id: str | None,
    matchday_id: int | None,
) -> tuple[dict[str, Any] | None, str]:
    # Best key: exact WSL fantasy match ID.
    if match_id:
        exact = [
            row for row in official_rows
            if str(row.get("match_id") or "") == str(match_id)
        ]
        if len(exact) == 1:
            return exact[0], "match_id"
        if len(exact) > 1:
            return exact[0], "match_id_ambiguous"

    # Safe fallback only when there is exactly one row for the matchday.
    if matchday_id is not None:
        same_day = [
            row for row in official_rows
            if row.get("matchday_id") == matchday_id
        ]
        if len(same_day) == 1:
            return same_day[0], "matchday_id"

    return None, "unmatched"


def delta(official: int | None, ours: int | None) -> int | None:
    if official is None or ours is None:
        return None
    return ours - official


def main() -> None:
    history = load_json(HISTORY_PATH, {})
    matches = history.get("matches", []) if isinstance(history, dict) else []

    if not matches:
        raise SystemExit("No matches found in involvement_history.json")

    player_meta: dict[str, dict[str, Any]] = {}
    for match in matches:
        for row in match.get("players", []) or []:
            player_id = row.get("player_id")
            if not player_id:
                continue
            player_meta.setdefault(str(player_id), {
                "player_id": str(player_id),
                "name": row.get("name"),
                "club": row.get("club"),
                "league": row.get("league"),
                "position": row.get("position"),
            })

    official_by_player: dict[str, list[dict[str, Any]]] = {}
    fetch_failures: list[dict[str, Any]] = []
    no_matchday_stats: list[dict[str, Any]] = []

    print(f"Fetching official WSL Fantasy detail for {len(player_meta)} players...")

    for index, (player_id, meta) in enumerate(sorted(player_meta.items()), start=1):
        try:
            payload = fetch_official_player(player_id)
            rows = normalize_official_rows(payload)
            official_by_player[player_id] = rows

            if not rows:
                no_matchday_stats.append({
                    "player_id": player_id,
                    "name": meta.get("name"),
                })

            print(
                f"[{index}/{len(player_meta)}] "
                f"{meta.get('name')}: {len(rows)} official matchday row(s)"
            )
        except Exception as exc:
            fetch_failures.append({
                "player_id": player_id,
                "name": meta.get("name"),
                "error": str(exc),
            })
            official_by_player[player_id] = []
            print(
                f"[{index}/{len(player_meta)}] "
                f"{meta.get('name')}: FAILED - {exc}"
            )

        # Be polite to the public feed.
        time.sleep(0.05)

    audit_rows: list[dict[str, Any]] = []
    unmatched_our_rows: list[dict[str, Any]] = []

    for match in matches:
        match_id = match.get("match_id")
        matchday_id = safe_int(match.get("matchday_id"))
        fixture = match.get("description")
        gw = match.get("fantasy_game_week")
        opta_match_id = match.get("opta_match_id")

        for ours in match.get("players", []) or []:
            player_id = ours.get("player_id")

            # Preserve unmatched Opta players in diagnostics, but they cannot
            # be queried from WSL Fantasy without a WSL player ID.
            if not player_id:
                unmatched_our_rows.append({
                    "fixture": fixture,
                    "fantasy_game_week": gw,
                    "opta_match_id": opta_match_id,
                    "opta_player_id": ours.get("opta_player_id"),
                    "name": ours.get("name"),
                    "attacking_actions": ours.get("attacking_actions"),
                    "defensive_actions": ours.get("defensive_actions"),
                })
                continue

            official_rows = official_by_player.get(str(player_id), [])
            official, match_method = match_official_row(
                official_rows,
                str(match_id) if match_id else None,
                matchday_id,
            )

            row = {
                "fantasy_game_week": str(gw) if gw is not None else None,
                "matchday_id": matchday_id,
                "fixture": fixture,
                "match_id": match_id,
                "opta_match_id": opta_match_id,
                "player_id": player_id,
                "opta_player_id": ours.get("opta_player_id"),
                "name": ours.get("name"),
                "club": ours.get("club"),
                "league": ours.get("league"),
                "position": ours.get("position"),
                "official_match_method": match_method,

                "official_attacking_actions":
                    official.get("attacking_actions") if official else None,
                "our_attacking_actions": safe_int(ours.get("attacking_actions")),

                "official_attacking_points":
                    official.get("attacking_points") if official else None,
                "our_attacking_points": safe_int(ours.get("attacking_points")),

                "official_defensive_actions":
                    official.get("defensive_actions") if official else None,
                "our_defensive_actions": safe_int(ours.get("defensive_actions")),

                "official_defensive_points":
                    official.get("defensive_points") if official else None,
                "our_defensive_points": safe_int(ours.get("defensive_points")),
            }

            row["attacking_actions_delta"] = delta(
                row["official_attacking_actions"],
                row["our_attacking_actions"],
            )
            row["attacking_points_delta"] = delta(
                row["official_attacking_points"],
                row["our_attacking_points"],
            )
            row["defensive_actions_delta"] = delta(
                row["official_defensive_actions"],
                row["our_defensive_actions"],
            )
            row["defensive_points_delta"] = delta(
                row["official_defensive_points"],
                row["our_defensive_points"],
            )

            row["official_row_found"] = official is not None
            row["exact_actions_match"] = (
                official is not None
                and row["attacking_actions_delta"] == 0
                and row["defensive_actions_delta"] == 0
            )
            row["exact_points_match"] = (
                official is not None
                and row["attacking_points_delta"] == 0
                and row["defensive_points_delta"] == 0
            )

            audit_rows.append(row)

    matched_rows = [r for r in audit_rows if r["official_row_found"]]
    action_exact = [r for r in matched_rows if r["exact_actions_match"]]
    points_exact = [r for r in matched_rows if r["exact_points_match"]]

    att_action_exact = [
        r for r in matched_rows if r["attacking_actions_delta"] == 0
    ]
    def_action_exact = [
        r for r in matched_rows if r["defensive_actions_delta"] == 0
    ]
    att_point_exact = [
        r for r in matched_rows if r["attacking_points_delta"] == 0
    ]
    def_point_exact = [
        r for r in matched_rows if r["defensive_points_delta"] == 0
    ]

    mismatch_rows = [
        r for r in matched_rows
        if not r["exact_actions_match"] or not r["exact_points_match"]
    ]

    delta_counts = {
        "attacking_actions": dict(Counter(
            str(r["attacking_actions_delta"])
            for r in matched_rows
            if r["attacking_actions_delta"] is not None
        )),
        "defensive_actions": dict(Counter(
            str(r["defensive_actions_delta"])
            for r in matched_rows
            if r["defensive_actions_delta"] is not None
        )),
        "attacking_points": dict(Counter(
            str(r["attacking_points_delta"])
            for r in matched_rows
            if r["attacking_points_delta"] is not None
        )),
        "defensive_points": dict(Counter(
            str(r["defensive_points_delta"])
            for r in matched_rows
            if r["defensive_points_delta"] is not None
        )),
    }

    output = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "WSL Fantasy official player detail feed vs involvement_history.json",
            "official_endpoint_pattern":
                "https://gaming.wslfootball.com/feeds/popup/stats/"
                "player_en_1_<player_id>.json",
            "history_generated_at_utc":
                history.get("metadata", {}).get("generated_at_utc"),
            "history_match_count": len(matches),
            "history_gameweeks":
                history.get("metadata", {}).get("available_gameweeks"),
            "unique_players_queried": len(player_meta),
            "official_fetch_failures": len(fetch_failures),
            "players_without_matchday_stats": len(no_matchday_stats),
            "our_rows_with_no_wsl_player_id": len(unmatched_our_rows),
            "player_match_rows": len(audit_rows),
            "official_player_match_rows_matched": len(matched_rows),
            "exact_attacking_actions": len(att_action_exact),
            "exact_defensive_actions": len(def_action_exact),
            "exact_attacking_points": len(att_point_exact),
            "exact_defensive_points": len(def_point_exact),
            "exact_both_action_totals": len(action_exact),
            "exact_both_point_totals": len(points_exact),
            "rows_with_any_mismatch": len(mismatch_rows),
        },
        "delta_counts": delta_counts,
        "fetch_failures": fetch_failures,
        "players_without_matchday_stats": no_matchday_stats,
        "our_rows_with_no_wsl_player_id": unmatched_our_rows,
        "mismatches": mismatch_rows,
        "all_rows": audit_rows,
    }

    OUTPUT_JSON.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    csv_fields = [
        "fantasy_game_week",
        "matchday_id",
        "fixture",
        "match_id",
        "opta_match_id",
        "name",
        "club",
        "league",
        "position",
        "player_id",
        "opta_player_id",
        "official_match_method",
        "official_row_found",
        "official_attacking_actions",
        "our_attacking_actions",
        "attacking_actions_delta",
        "official_attacking_points",
        "our_attacking_points",
        "attacking_points_delta",
        "official_defensive_actions",
        "our_defensive_actions",
        "defensive_actions_delta",
        "official_defensive_points",
        "our_defensive_points",
        "defensive_points_delta",
        "exact_actions_match",
        "exact_points_match",
    ]

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow({field: row.get(field) for field in csv_fields})

    print("")
    print("=== OFFICIAL WSL INVOLVEMENT AUDIT ===")
    print("History matches:", len(matches))
    print("Unique players queried:", len(player_meta))
    print("Official fetch failures:", len(fetch_failures))
    print("Players without matchdayStats:", len(no_matchday_stats))
    print("Our rows with no WSL player id:", len(unmatched_our_rows))
    print("Player-match rows:", len(audit_rows))
    print("Official rows matched:", len(matched_rows))

    if matched_rows:
        print(
            "ATT actions exact:",
            f"{len(att_action_exact)}/{len(matched_rows)}",
            f"({len(att_action_exact)/len(matched_rows):.1%})",
        )
        print(
            "DEF actions exact:",
            f"{len(def_action_exact)}/{len(matched_rows)}",
            f"({len(def_action_exact)/len(matched_rows):.1%})",
        )
        print(
            "ATT points exact:",
            f"{len(att_point_exact)}/{len(matched_rows)}",
            f"({len(att_point_exact)/len(matched_rows):.1%})",
        )
        print(
            "DEF points exact:",
            f"{len(def_point_exact)}/{len(matched_rows)}",
            f"({len(def_point_exact)/len(matched_rows):.1%})",
        )
        print(
            "Both action totals exact:",
            f"{len(action_exact)}/{len(matched_rows)}",
            f"({len(action_exact)/len(matched_rows):.1%})",
        )
        print(
            "Both point totals exact:",
            f"{len(points_exact)}/{len(matched_rows)}",
            f"({len(points_exact)/len(matched_rows):.1%})",
        )
        print("Rows with any mismatch:", len(mismatch_rows))

    print("")
    print("Wrote:", OUTPUT_JSON.name)
    print("Wrote:", OUTPUT_CSV.name)


if __name__ == "__main__":
    main()
