#!/usr/bin/env python3
"""
Test narrow event-definition hypotheses against the official WSL Fantasy
player-match involvement audit.

DIAGNOSTIC ONLY: this script does not modify production scoring.

Inputs:
  - official_wsl_involvement_audit.json
  - raw_opta_events/*.json

Output:
  - involvement_reconciliation_hypotheses.json

Hypotheses tested:
ATT:
  A) Count assist-flagged events that are not already key-pass flagged.
  B) Also count type-15 events carrying BOTH qualifiers 82 and 101.

DEF:
  A) Exclude type-12 clearance events carrying qualifier 185.
  B) For goalkeepers, exclude type-11 claim events with outcome 0.

The script compares current and revised totals against the official WSL
Fantasy attackingActions / defensiveActions for every matched player-match row.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
AUDIT_PATH = ROOT / "official_wsl_involvement_audit.json"
RAW_DIR = ROOT / "raw_opta_events"
OUTPUT_PATH = ROOT / "involvement_reconciliation_hypotheses.json"


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
        return int(event.get("outcome"))
    except (TypeError, ValueError):
        return 0


def qualifier_ids(event: dict[str, Any]) -> set[int]:
    qualifiers = event.get("qualifier") or event.get("qualifiers") or []
    if not isinstance(qualifiers, list):
        return set()

    result: set[int] = set()
    for qualifier in qualifiers:
        if not isinstance(qualifier, dict):
            continue
        try:
            result.add(int(qualifier.get("qualifierId")))
        except (TypeError, ValueError):
            pass
    return result


def is_key_pass(event: dict[str, Any]) -> bool:
    return any(
        event.get(key) in (1, "1", True, "true", "True")
        for key in ("keypass", "keyPass", "key_pass")
    )


def is_assist(event: dict[str, Any]) -> bool:
    return event.get("assist") in (1, "1", True, "true", "True")


def live_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = (payload.get("liveData") or {}).get("event") or []
    return events if isinstance(events, list) else []


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "eventId": event.get("eventId"),
        "typeId": event.get("typeId"),
        "periodId": event.get("periodId"),
        "timeMin": event.get("timeMin"),
        "timeSec": event.get("timeSec"),
        "outcome": event.get("outcome"),
        "assist": event.get("assist"),
        "keypass": event.get("keypass"),
        "keyPass": event.get("keyPass"),
        "key_pass": event.get("key_pass"),
        "qualifier_ids": sorted(qualifier_ids(event)),
        "playerId": event.get("playerId"),
        "playerName": event.get("playerName"),
    }


def main() -> None:
    if not AUDIT_PATH.exists():
        raise SystemExit("official_wsl_involvement_audit.json not found")
    if not RAW_DIR.exists():
        raise SystemExit("raw_opta_events directory not found")

    audit = load_json(AUDIT_PATH)
    rows = [
        row for row in audit.get("all_rows", [])
        if row.get("official_row_found")
    ]

    if not rows:
        raise SystemExit("No matched player-match rows found in official audit")

    raw_by_match: dict[str, dict[str, Any]] = {}
    for path in RAW_DIR.glob("*.json"):
        raw_by_match[path.stem] = load_json(path)

    missing_raw_matches = sorted({
        str(row.get("opta_match_id") or "")
        for row in rows
        if str(row.get("opta_match_id") or "") not in raw_by_match
    })
    if missing_raw_matches:
        raise SystemExit(
            "Missing raw Opta payload(s): " + ", ".join(missing_raw_matches)
        )

    current_att_exact = 0
    revised_att_assist_only_exact = 0
    revised_att_full_exact = 0

    current_def_exact = 0
    revised_def_clearance_only_exact = 0
    revised_def_full_exact = 0

    att_current_deltas = Counter()
    att_assist_only_deltas = Counter()
    att_full_deltas = Counter()

    def_current_deltas = Counter()
    def_clearance_only_deltas = Counter()
    def_full_deltas = Counter()

    affected_rows: list[dict[str, Any]] = []
    remaining_att_after_assist_only: list[dict[str, Any]] = []
    remaining_att_after_full: list[dict[str, Any]] = []
    remaining_def_after_clearance_only: list[dict[str, Any]] = []
    remaining_def_after_full: list[dict[str, Any]] = []

    for row in rows:
        opta_match_id = str(row.get("opta_match_id") or "")
        opta_player_id = str(row.get("opta_player_id") or "")
        payload = raw_by_match[opta_match_id]

        events = [
            event for event in live_events(payload)
            if event_player_id(event) == opta_player_id
        ]

        assist_not_keypass_events = [
            event for event in events
            if is_assist(event) and not is_key_pass(event)
        ]

        q82_q101_type15_events = [
            event for event in events
            if (
                event_type(event) == 15
                and 82 in qualifier_ids(event)
                and 101 in qualifier_ids(event)
            )
        ]

        q185_clearance_events = [
            event for event in events
            if event_type(event) == 12 and 185 in qualifier_ids(event)
        ]

        failed_claim_events = [
            event for event in events
            if event_type(event) == 11 and event_outcome(event) == 0
        ]

        official_att = int(row.get("official_attacking_actions") or 0)
        current_att = int(row.get("our_attacking_actions") or 0)
        assist_only_att = current_att + len(assist_not_keypass_events)
        full_att = assist_only_att + len(q82_q101_type15_events)

        official_def = int(row.get("official_defensive_actions") or 0)
        current_def = int(row.get("our_defensive_actions") or 0)
        clearance_only_def = current_def - len(q185_clearance_events)

        full_def = clearance_only_def
        if str(row.get("position") or "").upper() == "GK":
            full_def -= len(failed_claim_events)

        cur_att_delta = current_att - official_att
        assist_delta = assist_only_att - official_att
        full_att_delta = full_att - official_att

        cur_def_delta = current_def - official_def
        clearance_delta = clearance_only_def - official_def
        full_def_delta = full_def - official_def

        att_current_deltas[cur_att_delta] += 1
        att_assist_only_deltas[assist_delta] += 1
        att_full_deltas[full_att_delta] += 1

        def_current_deltas[cur_def_delta] += 1
        def_clearance_only_deltas[clearance_delta] += 1
        def_full_deltas[full_def_delta] += 1

        current_att_exact += int(cur_att_delta == 0)
        revised_att_assist_only_exact += int(assist_delta == 0)
        revised_att_full_exact += int(full_att_delta == 0)

        current_def_exact += int(cur_def_delta == 0)
        revised_def_clearance_only_exact += int(clearance_delta == 0)
        revised_def_full_exact += int(full_def_delta == 0)

        if assist_delta != 0:
            remaining_att_after_assist_only.append({
                "fantasy_game_week": row.get("fantasy_game_week"),
                "fixture": row.get("fixture"),
                "name": row.get("name"),
                "position": row.get("position"),
                "official_attacking_actions": official_att,
                "current_attacking_actions": current_att,
                "assist_only_attacking_actions": assist_only_att,
                "delta_after_assist_only": assist_delta,
                "q82_q101_type15_events": [
                    serialize_event(event)
                    for event in q82_q101_type15_events
                ],
            })

        if full_att_delta != 0:
            remaining_att_after_full.append({
                "fantasy_game_week": row.get("fantasy_game_week"),
                "fixture": row.get("fixture"),
                "name": row.get("name"),
                "official_attacking_actions": official_att,
                "revised_attacking_actions": full_att,
                "delta": full_att_delta,
            })

        if clearance_delta != 0:
            remaining_def_after_clearance_only.append({
                "fantasy_game_week": row.get("fantasy_game_week"),
                "fixture": row.get("fixture"),
                "name": row.get("name"),
                "position": row.get("position"),
                "official_defensive_actions": official_def,
                "current_defensive_actions": current_def,
                "clearance_only_defensive_actions": clearance_only_def,
                "delta_after_clearance_only": clearance_delta,
                "failed_claim_events": [
                    serialize_event(event)
                    for event in failed_claim_events
                ],
            })

        if full_def_delta != 0:
            remaining_def_after_full.append({
                "fantasy_game_week": row.get("fantasy_game_week"),
                "fixture": row.get("fixture"),
                "name": row.get("name"),
                "official_defensive_actions": official_def,
                "revised_defensive_actions": full_def,
                "delta": full_def_delta,
            })

        if (
            assist_not_keypass_events
            or q82_q101_type15_events
            or q185_clearance_events
            or (
                str(row.get("position") or "").upper() == "GK"
                and failed_claim_events
            )
        ):
            affected_rows.append({
                "fantasy_game_week": row.get("fantasy_game_week"),
                "fixture": row.get("fixture"),
                "name": row.get("name"),
                "club": row.get("club"),
                "position": row.get("position"),
                "opta_match_id": opta_match_id,
                "opta_player_id": opta_player_id,

                "official_attacking_actions": official_att,
                "current_attacking_actions": current_att,
                "revised_attacking_actions": full_att,
                "attacking_delta_before": cur_att_delta,
                "attacking_delta_after": full_att_delta,

                "official_defensive_actions": official_def,
                "current_defensive_actions": current_def,
                "revised_defensive_actions": full_def,
                "defensive_delta_before": cur_def_delta,
                "defensive_delta_after": full_def_delta,

                "assist_not_keypass_count": len(assist_not_keypass_events),
                "q82_q101_type15_count": len(q82_q101_type15_events),
                "q185_clearance_count": len(q185_clearance_events),
                "failed_gk_claim_count": (
                    len(failed_claim_events)
                    if str(row.get("position") or "").upper() == "GK"
                    else 0
                ),

                "assist_not_keypass_events": [
                    serialize_event(event)
                    for event in assist_not_keypass_events
                ],
                "q82_q101_type15_events": [
                    serialize_event(event)
                    for event in q82_q101_type15_events
                ],
                "q185_clearance_events": [
                    serialize_event(event)
                    for event in q185_clearance_events
                ],
                "failed_gk_claim_events": [
                    serialize_event(event)
                    for event in failed_claim_events
                ] if str(row.get("position") or "").upper() == "GK" else [],
            })

    total = len(rows)

    result = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "source_audit": AUDIT_PATH.name,
            "raw_match_count": len(raw_by_match),
            "player_match_rows": total,
            "diagnostic_only": True,
        },
        "hypotheses": {
            "attacking": {
                "current_definition_exact": current_att_exact,
                "current_definition_rate": current_att_exact / total,
                "assist_not_keypass_only_exact":
                    revised_att_assist_only_exact,
                "assist_not_keypass_only_rate":
                    revised_att_assist_only_exact / total,
                "full_revised_exact": revised_att_full_exact,
                "full_revised_rate": revised_att_full_exact / total,
                "rule_1": (
                    "Count assist-flagged events when the same event is not "
                    "already flagged as a key pass."
                ),
                "rule_2": (
                    "Count type-15 events carrying BOTH qualifiers 82 and 101 "
                    "instead of excluding that specific subtype with the other "
                    "qualifier-82 events."
                ),
                "current_delta_counts":
                    dict(sorted(att_current_deltas.items())),
                "assist_only_delta_counts":
                    dict(sorted(att_assist_only_deltas.items())),
                "full_revised_delta_counts":
                    dict(sorted(att_full_deltas.items())),
                "remaining_after_assist_only":
                    remaining_att_after_assist_only,
                "remaining_after_full_revision":
                    remaining_att_after_full,
            },
            "defensive": {
                "current_definition_exact": current_def_exact,
                "current_definition_rate": current_def_exact / total,
                "exclude_q185_clearance_only_exact":
                    revised_def_clearance_only_exact,
                "exclude_q185_clearance_only_rate":
                    revised_def_clearance_only_exact / total,
                "full_revised_exact": revised_def_full_exact,
                "full_revised_rate": revised_def_full_exact / total,
                "rule_1": (
                    "Do not count type-12 clearance events carrying "
                    "qualifier 185."
                ),
                "rule_2": (
                    "For goalkeepers, do not count type-11 claim events "
                    "with outcome 0."
                ),
                "current_delta_counts":
                    dict(sorted(def_current_deltas.items())),
                "clearance_only_delta_counts":
                    dict(sorted(def_clearance_only_deltas.items())),
                "full_revised_delta_counts":
                    dict(sorted(def_full_deltas.items())),
                "remaining_after_clearance_only":
                    remaining_def_after_clearance_only,
                "remaining_after_full_revision":
                    remaining_def_after_full,
            },
        },
        "affected_rows": affected_rows,
    }

    OUTPUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=== INVOLVEMENT RECONCILIATION HYPOTHESES ===")
    print("Player-match rows:", total)
    print("")
    print(
        "ATT current exact:",
        f"{current_att_exact}/{total}"
    )
    print(
        "ATT + assist-not-keypass exact:",
        f"{revised_att_assist_only_exact}/{total}"
    )
    print(
        "ATT full revised exact:",
        f"{revised_att_full_exact}/{total}"
    )
    print(
        "ATT remaining after full revision:",
        len(remaining_att_after_full)
    )
    print("")
    print(
        "DEF current exact:",
        f"{current_def_exact}/{total}"
    )
    print(
        "DEF excluding type12+q185 exact:",
        f"{revised_def_clearance_only_exact}/{total}"
    )
    print(
        "DEF full revised exact:",
        f"{revised_def_full_exact}/{total}"
    )
    print(
        "DEF remaining after full revision:",
        len(remaining_def_after_full)
    )
    print("")
    print("Wrote:", OUTPUT_PATH.name)


if __name__ == "__main__":
    main()
