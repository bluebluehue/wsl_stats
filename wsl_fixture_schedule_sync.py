"""Reconcile WSL fantasy fixture kickoffs against the main WSL website.

Import reconcile_fantasy_fixture_feed and call it on the RAW fixtures list
before normalize_fixture, canonical gameweek assignment, and player builds.

Only kickoff timestamps are altered; original IDs, scores, gameweeks,
ratings and other fantasy fields stay intact.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

WEBSITE_URL = "https://www.wslfootball.com/fixtures/index"
COMPETITIONS = {
    "wpll::Football_Competition::e32284e8a1214f1ca83a3245d690b336": "barclays-wsl",
    "wpll::Football_Competition::422757a2c70d450eba118ad97bed5222": "barclays-wsl2",
}
PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.DOTALL)


def website_matches_from_html(page_html: str) -> list[dict[str, Any]]:
    """Extract structured initialMatches from Next.js Flight payload.

    The HTML contains many script chunks. Their payload strings must be JSON-
    decoded and concatenated BEFORE decoding initialMatches, because chunk
    boundaries can occur in the middle of JSON text.
    """
    parts = []
    for raw in PUSH_RE.findall(page_html):
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, list) and len(entry) > 1 and entry[0] == 1 and isinstance(entry[1], str):
            parts.append(entry[1])
    stream = "".join(parts)
    marker = '"initialMatches":'
    offset = stream.find(marker)
    if offset < 0:
        raise ValueError("Website response missing initialMatches Next.js payload")
    result, _ = json.JSONDecoder().raw_decode(stream[offset + len(marker):].lstrip())
    if not isinstance(result, list) or len(result) < 100:
        raise ValueError("Website initialMatches is missing or unexpectedly short")
    return result


def fetch_website_matches(session: requests.Session | None = None) -> list[dict[str, Any]]:
    client = session or requests
    response = client.get(
        WEBSITE_URL,
        timeout=50,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; WSL schedule sync/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Cache-Control": "no-cache",
        },
    )
    response.raise_for_status()
    return website_matches_from_html(response.text)


def _canonical_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Both source timestamps are documented UTC.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def reconcile_fantasy_fixture_feed(
    fantasy_fixtures: list[dict[str, Any]],
    website_matches: list[dict[str, Any]],
    *,
    min_match_ratio: float = 0.90,
) -> dict[str, Any]:
    """Update fantasy fixtures IN PLACE; return an auditable report.

    Safety: reject unexpected feed coverage, duplicate IDs, team mismatches,
    competition mismatches, unknown kickoffs. A fixture is never matched by
    date or team name alone.
    """
    website_by_id: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for match in website_matches:
        match_id = str(match.get("matchId") or "")
        if not match_id:
            continue
        if match_id in website_by_id:
            duplicates.add(match_id)
        website_by_id[match_id] = match
    if duplicates:
        raise ValueError(f"Duplicate website match IDs: {sorted(duplicates)[:5]}")

    league_rows = [f for f in fantasy_fixtures if f.get("competitionId") in COMPETITIONS]
    valid: list[tuple[dict[str, Any], str, str]] = []
    mismatches: list[dict[str, Any]] = []
    unmatched: list[str] = []
    unknown_time: list[str] = []
    for fixture in league_rows:
        match_id = str(fixture.get("matchId") or "")
        website = website_by_id.get(match_id)
        if not website:
            unmatched.append(match_id)
            continue
        website_comp = (website.get("matchSet") or {}).get("competitionId")
        if website_comp != fixture.get("competitionId"):
            raise ValueError(f"Competition mismatch for {match_id}")
        wh = (website.get("home") or {}).get("teamId")
        wa = (website.get("away") or {}).get("teamId")
        if wh != fixture.get("homeTeamId") or wa != fixture.get("awayTeamId"):
            raise ValueError(f"Home/away team mismatch for {match_id}")
        new = _canonical_utc(website.get("matchDateUtc"))
        old = _canonical_utc(fixture.get("matchDateTimeUtc"))
        if not new or website.get("isUnknownKickOffTime") is True:
            unknown_time.append(match_id)
            continue
        if not old:
            raise ValueError(f"Invalid fantasy kickoff for {match_id}")
        valid.append((fixture, old, new))

    if not league_rows or len(valid) / len(league_rows) < min_match_ratio:
        raise ValueError(
            f"Website schedule only verified {len(valid)}/{len(league_rows)} league fixtures; "
            "declining to modify data"
        )

    for fixture, old, new in valid:
        if old != new:
            mismatches.append({
                "match_id": fixture["matchId"],
                "home": fixture.get("homeAcronymName"),
                "away": fixture.get("awayAcronymName"),
                "fantasy_utc": old,
                "website_utc": new,
            })
            fixture["matchDateTimeUtc"] = new

    return {
        "website_url": WEBSITE_URL,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "website_matches_received": len(website_matches),
        "fantasy_league_fixtures": len(league_rows),
        "matched_with_confirmed_kickoff": len(valid),
        "unchanged": len(valid) - len(mismatches),
        "corrected": len(mismatches),
        "corrections": mismatches,
        "unmatched_count": len(unmatched),
        "unmatched_ids": unmatched[:30],
        "unknown_kickoff_count": len(unknown_time),
        "unknown_kickoff_ids": unknown_time[:30],
    }
