#!/usr/bin/env python3
"""
Explain any remaining official-vs-production involvement mismatches at the
individual Opta-event level.

DIAGNOSTIC ONLY. Does not modify production data or scoring.

Inputs:
  - official_wsl_involvement_audit.json
  - raw_opta_events/*.json

Output:
  - remaining_involvement_mismatch_events.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
AUDIT_PATH = ROOT / "official_wsl_involvement_audit.json"
RAW_DIR = ROOT / "raw_opta_events"
OUTPUT_PATH = ROOT / "remaining_involvement_mismatch_events.json"

EVENT_NAMES = {
    1: "Pass",
    3: "Take-on / dribble",
    7: "Tackle",
    8: "Interception",
    10: "Defensive save/block event",
    11: "Goalkeeper claim",
    12: "Clearance",
    15: "Attempt saved / blocked shot event",
    16: "Goal",
    41: "Goalkeeper punch",
    49: "Ball recovery",
    52: "Goalkeeper pickup",
}

QUALIFIER_NAMES = {
    2: "Cross",
    28: "Own goal",
    82: "Blocked shot",
    94: "Defensive block",
    101: "Saved off line",
    185: "Blocked cross",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_id(value: Any) -> str:
    return str(value or "").rsplit(":", 1)[-1]


def event_player_id(event: dict[str, Any]) -> str:
    for key in ("playerId", "participantId", "player_id", "participant_id"):
        if event.get(key):
            return compact_id(event.get(key))
    return ""


def event_type(event: dict[str, Any]) -> int | None:
    try:
        return int(event.get("typeId"))
    except (TypeError, ValueError):
        return None


def event_outcome(event: dict[str, Any]) -> int:
    try:
        return int(event.get("outcome") or 0)
    except (TypeError, ValueError):
        return 0


def qualifier_ids(event: dict[str, Any]) -> set[int]:
    qualifiers = event.get("qualifier") or event.get("qualifiers") or []
    out: set[int] = set()
    if isinstance(qualifiers, list):
        for q in qualifiers:
            if not isinstance(q, dict):
                continue
            try:
                out.add(int(q.get("qualifierId")))
            except (TypeError, ValueError):
                pass
    return out


def counted_defensive_reason(event: dict[str, Any], position: str) -> str | None:
    typ = event_type(event)
    outcome = event_outcome(event)
    qids = qualifier_ids(event)
    is_gk = position.upper() == "GK"

    if typ == 7 and outcome == 1:
        return "Tackle won"
    if typ == 8:
        return "Interception"
    if typ == 12 and 185 not in qids:
        return "Clearance"
    if typ == 10 and 94 in qids:
        return "Blocked shot"
    if typ == 49:
        return "Recovery"

    if is_gk:
        if typ == 11 and outcome != 0:
            return "Successful goalkeeper claim"
        if typ == 41:
            return "Goalkeeper punch"
        if typ == 52:
            return "Goalkeeper pickup"

    return None


def serialize_event(event: dict[str, Any], reason: str | None) -> dict[str, Any]:
    qids = sorted(qualifier_ids(event))
    return {
        "eventId": event.get("eventId"),
        "id": event.get("id"),
        "minute": event.get("timeMin"),
        "second": event.get("timeSec"),
        "typeId": event.get("typeId"),
        "type_name": EVENT_NAMES.get(event_type(event), "Other"),
        "outcome": event.get("outcome"),
        "counted_as": reason,
        "x": event.get("x"),
        "y": event.get("y"),
        "qualifiers": [
            {
                "id": qid,
                "name": QUALIFIER_NAMES.get(qid, "")
            }
            for qid in qids
        ],
    }


def main() -> None:
    if not AUDIT_PATH.exists():
        raise SystemExit("official_wsl_involvement_audit.json not found")
    if not RAW_DIR.exists():
        raise SystemExit("raw_opta_events directory not found")

    audit = load_json(AUDIT_PATH)
    mismatches = audit.get("mismatches") or []

    if not mismatches:
        print("No mismatches remain.")
        OUTPUT_PATH.write_text(
            json.dumps({"mismatches": []}, indent=2),
            encoding="utf-8",
        )
        return

    output_rows = []

    print("=== REMAINING INVOLVEMENT MISMATCHES ===")
    print("Mismatch rows:", len(mismatches))
    print("")

    for row in mismatches:
        opta_match_id = str(row.get("opta_match_id") or "")
        opta_player_id = compact_id(row.get("opta_player_id"))
        position = str(row.get("position") or "")

        raw_path = RAW_DIR / f"{opta_match_id}.json"
        if not raw_path.exists():
            print(
                f"{row.get('name')}: raw file missing for match {opta_match_id}"
            )
            continue

        payload = load_json(raw_path)
        events = (payload.get("liveData") or {}).get("event") or []

        player_events = [
            event for event in events
            if event_player_id(event) == opta_player_id
        ]

        counted = []
        nearby_defensive = []

        for event in player_events:
            reason = counted_defensive_reason(event, position)
            if reason:
                counted.append(serialize_event(event, reason))

            typ = event_type(event)
            # Include all defensive-ish event families for context, even if
            # production does not count them.
            if typ in (7, 8, 10, 11, 12, 32, 41, 49, 52, 54, 59, 61, 74):
                nearby_defensive.append(serialize_event(event, reason))

        result = {
            "fantasy_game_week": row.get("fantasy_game_week"),
            "fixture": row.get("fixture"),
            "name": row.get("name"),
            "club": row.get("club"),
            "position": position,
            "opta_match_id": opta_match_id,
            "opta_player_id": opta_player_id,
            "official_defensive_actions":
                row.get("official_defensive_actions"),
            "our_defensive_actions":
                row.get("our_defensive_actions"),
            "defensive_delta":
                row.get("defensive_actions_delta"),
            "counted_defensive_events": counted,
            "all_defensiveish_events": nearby_defensive,
        }
        output_rows.append(result)

        print(
            f"{row.get('name')} | {row.get('fixture')} | "
            f"official DEF {row.get('official_defensive_actions')} / "
            f"ours {row.get('our_defensive_actions')}"
        )
        print("Events our production parser counts:")
        for event in counted:
            qtext = ", ".join(
                f"{q['id']} {q['name']}".strip()
                for q in event["qualifiers"]
            ) or "none"
            print(
                f"  - {event['minute']}:{str(event['second']).zfill(2)} | "
                f"{event['counted_as']} | "
                f"type {event['typeId']} ({event['type_name']}) | "
                f"outcome {event['outcome']} | qualifiers: {qtext}"
            )
        print("")
        print("All defensive-ish Opta events for context:")
        for event in nearby_defensive:
            qtext = ", ".join(
                f"{q['id']} {q['name']}".strip()
                for q in event["qualifiers"]
            ) or "none"
            counted_marker = "COUNTED" if event["counted_as"] else "not counted"
            print(
                f"  - {event['minute']}:{str(event['second']).zfill(2)} | "
                f"type {event['typeId']} ({event['type_name']}) | "
                f"outcome {event['outcome']} | {counted_marker} | "
                f"qualifiers: {qtext}"
            )
        print("")

    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "mismatch_count": len(output_rows),
                "mismatches": output_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Wrote:", OUTPUT_PATH.name)


if __name__ == "__main__":
    main()
