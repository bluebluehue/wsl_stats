"""Build WSL Fantasy player data in the same broad shape as the NWSL stats project.

This script reads the public JSON feeds used by the WSL Fantasy create-team UI
and writes transformed_data.json for the included static table viewer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

BASE_URL = "https://gaming.wslfootball.com"
TOUR_ID = 1
LANGUAGE = "en"
MATCHDAY_ID = int(os.getenv("WSL_MATCHDAY_ID", "1"))

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "player_history.json"
TRANSFORMED_PATH = ROOT / "transformed_data.json"
FIXTURES_PATH = ROOT / "fixtures.json"
TEAMS_PATH = ROOT / "teams.json"
RAW_DIR = ROOT / "raw_feeds"

URLS = {
    "config": f"/feeds/config/web/configurations.json",
    "tour": f"/feeds/tour/details/{TOUR_ID}.json",
    "teams": f"/feeds/filters/teams/competition/{LANGUAGE}_{TOUR_ID}.json",
    "fixtures": f"/feeds/fixtures/fixtures_{LANGUAGE}_{TOUR_ID}.json?v=3",
    "players": f"/feeds/players/matchday_{LANGUAGE}_{TOUR_ID}_{MATCHDAY_ID}.json?v=3",
}

POSITION_MAP = {
    "gk": "GK",
    "def": "DEF",
    "mid": "MID",
    "fwd": "FOR",
    "for": "FOR",
}

# The NWSL viewer expects numbered week columns. WSL/WSL2 regular season should
# fit inside 22 matchdays, but keeping 30 avoids breaking if cup/double-week
# structures appear later.
MAX_MATCHDAY_COLUMNS = int(os.getenv("WSL_MAX_MATCHDAYS", "30"))


def fetch_json(path: str, cache_name: str | None = None, from_local: bool = False) -> dict[str, Any]:
    """Fetch one WSL JSON feed, optionally from raw_feeds for offline testing."""
    RAW_DIR.mkdir(exist_ok=True)
    local_path = RAW_DIR / (cache_name or Path(path.split("?", 1)[0]).name)

    if from_local and local_path.exists():
        return json.loads(local_path.read_text(encoding="utf-8"))

    url = urljoin(BASE_URL, path)
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 WSL fantasy stats parser",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.wslfootball.com/fantasy/create-team",
        },
    )
    response.raise_for_status()
    data = response.json()
    local_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def value_of(feed: dict[str, Any]) -> Any:
    return feed.get("Data", {}).get("Value")


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.replace("%", "").replace("£", "").replace("m", "")
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(safe_float(value, float(default))))
    except (TypeError, ValueError):
        return default


def compact_id(raw_id: str | None) -> str:
    if not raw_id:
        return ""
    return raw_id.rsplit("::", 1)[-1]


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_fixture_date(value: str | None) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    # Keep the NWSL viewer's compact style: 4 Sep, 13 Sep, etc.
    return f"{dt.day} {dt.strftime('%b')}"


def format_fixture_time(value: str | None) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    return dt.strftime("%H:%M")


def normalize_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "match_id": fixture.get("matchId"),
        "provider_id": fixture.get("providerId"),
        "competition_id": fixture.get("competitionId"),
        "status": fixture.get("status"),
        "provider_status": fixture.get("providerStatus"),
        "match_date_time_utc": fixture.get("matchDateTimeUtc"),
        "game_date": format_fixture_date(fixture.get("matchDateTimeUtc")),
        "kick_off_time": format_fixture_time(fixture.get("matchDateTimeUtc")),
        "game_week": str(fixture.get("matchdayId") or ""),
        "gameday_id": fixture.get("gamedayId"),
        "home_id": fixture.get("homeAcronymName") or compact_id(fixture.get("homeTeamId")),
        "home_team_id": fixture.get("homeTeamId"),
        "home_provider_id": fixture.get("homeProviderId"),
        "home_name": fixture.get("homeOfficialName") or fixture.get("homeMediaName"),
        "home_short_name": fixture.get("homeShortName") or fixture.get("homeMediaShortName"),
        "home_rating": fixture.get("homeRating"),
        "away_id": fixture.get("awayAcronymName") or compact_id(fixture.get("awayTeamId")),
        "away_team_id": fixture.get("awayTeamId"),
        "away_provider_id": fixture.get("awayProviderId"),
        "away_name": fixture.get("awayOfficialName") or fixture.get("awayMediaName"),
        "away_short_name": fixture.get("awayShortName") or fixture.get("awayMediaShortName"),
        "away_rating": fixture.get("awayRating"),
        "home_score": fixture.get("homeScore"),
        "away_score": fixture.get("awayScore"),
        "deadline_date": fixture.get("deadlineDate"),
    }


def normalize_team(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "team_id": team.get("teamId"),
        "provider_id": team.get("providerId"),
        "competition_id": team.get("competitionId"),
        "name": team.get("officialName") or team.get("mediaName"),
        "short_name": team.get("shortName") or team.get("mediaShortName"),
        "acronym": team.get("acronymName"),
    }


def load_history() -> dict[str, dict[str, dict[str, float]]]:
    if not HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_history(players: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]]:
    history = load_history()
    today = datetime.now(timezone.utc).date().isoformat()
    for player in players:
        key = player["Name"]
        history.setdefault(key, {})[today] = {
            "Value": safe_float(player.get("Value")),
            "Selected Percentage": safe_float(player.get("Selected Percentage")),
        }
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return history


def selected_delta(history: dict[str, Any], name: str, current: float, days_back: int = 7) -> float:
    snapshots = history.get(name, {})
    if not snapshots:
        return 0.0
    dates = sorted(snapshots)
    if len(dates) < 2:
        return 0.0
    # Prefer a snapshot at least N days old; otherwise use oldest available.
    current_date = datetime.now(timezone.utc).date()
    baseline_date = dates[0]
    for d in dates:
        try:
            age = (current_date - datetime.fromisoformat(d).date()).days
        except ValueError:
            continue
        if age >= days_back:
            baseline_date = d
    baseline = safe_float(snapshots.get(baseline_date, {}).get("Selected Percentage"))
    return round(current - baseline, 2)


def detect_last_global_price_change_date(history: dict[str, Any]) -> str | None:
    changed_dates: list[str] = []
    for snapshots in history.values():
        dates = sorted(snapshots)
        for prev, cur in zip(dates, dates[1:]):
            if safe_float(snapshots[prev].get("Value")) != safe_float(snapshots[cur].get("Value")):
                changed_dates.append(cur)
    return max(changed_dates) if changed_dates else None


def selected_delta_since(history: dict[str, Any], name: str, current: float, since_date: str | None) -> float:
    if not since_date:
        return 0.0
    snapshots = history.get(name, {})
    if since_date not in snapshots:
        return 0.0
    return round(current - safe_float(snapshots[since_date].get("Selected Percentage")), 2)


def fixture_rating_to_score(rating: float | None) -> int | str:
    """Convert WSL's 0-100-ish currentRating into the 1-5 score style used by the viewer.

    Higher appears easier/better in the WSL feed. The table's historical fixture
    score convention uses 1 as best/easiest and 5 as hardest, so invert it.
    """
    if rating is None:
        return "-"
    r = safe_float(rating, 50.0)
    if r >= 85:
        return 1
    if r >= 75:
        return 2
    if r >= 65:
        return 3
    if r >= 55:
        return 4
    return 5


def build_upcoming_fixture(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "game_date": format_fixture_date(raw.get("matchDateTimeUtc")),
        "kick_off_time": format_fixture_time(raw.get("matchDateTimeUtc")),
        "game_week": str(raw.get("matchdayId") or ""),
        "match_id": raw.get("matchId"),
        "opponent_id": raw.get("vsTeamAcronymName") or compact_id(raw.get("vsTeamId")),
        "opponent_name": raw.get("vsTeamName"),
        "opponent_short_name": raw.get("vsTeamShortName"),
        "location": raw.get("location"),
        "current_rating": raw.get("currentRating"),
        # Compatibility keys from the NWSL data shape. For a player-centric WSL
        # fixture record we do not know home/away teams here, but the frontend and
        # exports can still use opponent/location.
        "home_id": raw.get("vsTeamAcronymName") if raw.get("location") == "A" else "",
        "home_name": raw.get("vsTeamName") if raw.get("location") == "A" else "",
        "home_short_name": raw.get("vsTeamShortName") if raw.get("location") == "A" else "",
        "away_id": raw.get("vsTeamAcronymName") if raw.get("location") == "H" else "",
        "away_name": raw.get("vsTeamName") if raw.get("location") == "H" else "",
        "away_short_name": raw.get("vsTeamShortName") if raw.get("location") == "H" else "",
    }


def fixture_details_text(fixtures: list[dict[str, Any]], position: str) -> str:
    if not fixtures:
        return "No upcoming fixture in feed."
    lines = ["Upcoming fixtures from WSL feed:"]
    for f in fixtures:
        opponent = f.get("opponent_id") or f.get("opponent_short_name") or f.get("opponent_name") or "TBD"
        loc = f.get("location") or ""
        rating = f.get("current_rating")
        score = fixture_rating_to_score(rating)
        lines.append(
            f"GW{f.get('game_week')}: vs {opponent} ({loc}) on {f.get('game_date')} {f.get('kick_off_time')}"
        )
        if rating is not None:
            lines.append(f"  WSL currentRating: {rating}/100; table score: {score}/5")
        if position in {"GK", "DEF"}:
            lines.append("  Defensive fixture value currently uses WSL currentRating until a WSL xG model is added.")
        else:
            lines.append("  Attacking fixture value currently uses WSL currentRating until a WSL xG model is added.")
    ratings = [safe_float(f.get("current_rating"), math.nan) for f in fixtures if f.get("current_rating") is not None]
    ratings = [r for r in ratings if not math.isnan(r)]
    if ratings:
        lines.append(f"Average WSL currentRating: {round(sum(ratings) / len(ratings), 1)}/100")
    return "\n".join(lines)


def recommendation(player: dict[str, Any]) -> str:
    total = safe_float(player.get("Total Points"))
    last_season = safe_float(player.get("Previous Season Points"))
    selected = safe_float(player.get("Selected Percentage"))
    value = max(safe_float(player.get("Value")), 0.1)
    ppm_prev = last_season / value if value else 0.0
    next_rating = safe_float(player.get("Next Fixture Rating"), 0.0)

    if selected >= 35 and (last_season >= 120 or total >= 80):
        return "Template"
    if selected < 15 and (ppm_prev >= 12 or next_rating >= 75):
        return "Differential"
    if selected < 5 and (ppm_prev >= 8 or next_rating >= 70):
        return "Punt"
    if last_season >= 100 or total >= 70 or next_rating >= 80:
        return "Watchlist"
    return "Monitor"


def form_rating(player: dict[str, Any]) -> int:
    # Preseason feeds expose prior-season points and current ownership more
    # reliably than current form. Once form/averagePoints populate, this starts
    # using them automatically.
    form = player.get("Raw Form")
    avg = player.get("Average Points")
    if form not in (None, ""):
        return min(100, max(0, round(safe_float(form) * 12)))
    if avg not in (None, ""):
        return min(100, max(0, round(safe_float(avg) * 15)))
    value = max(safe_float(player.get("Value")), 0.1)
    prior = safe_float(player.get("Previous Season Points"))
    return min(100, max(0, round((prior / value) * 4)))


def decision_rating(player: dict[str, Any]) -> float:
    pos = player.get("Position")
    fixture = safe_float(player.get("Next Fixture Rating"), 50.0)
    form = safe_float(player.get("Form Rating"), 50.0)
    if pos == "GK":
        weights = (0.85, 0.15)
    elif pos == "DEF":
        weights = (0.65, 0.35)
    elif pos == "MID":
        weights = (0.35, 0.65)
    else:
        weights = (0.25, 0.75)
    return round(fixture * weights[0] + form * weights[1], 1)


def transform_player(raw: dict[str, Any]) -> dict[str, Any]:
    name = f"{raw.get('mediaFirstName', '').strip()} {raw.get('mediaLastName', '').strip()}".strip()
    short_name = raw.get("mediaShortName") or name
    position = POSITION_MAP.get(str(raw.get("skillName") or "").lower(), str(raw.get("skillName") or "").upper())
    value = safe_float(raw.get("valuation"))
    selected = safe_float(raw.get("selectedPercentage"))
    total_points = safe_float(raw.get("totalPoints"))
    prior_points = safe_float(raw.get("pointsLastSeason"))
    avg_points = raw.get("averagePoints")
    raw_form = raw.get("form")
    upcoming = [build_upcoming_fixture(f) for f in raw.get("upcomingFixtures", [])]
    next_fixture = upcoming[0] if upcoming else None
    following_fixture = upcoming[1] if len(upcoming) > 1 else None
    next_rating = safe_float(next_fixture.get("current_rating"), 0.0) if next_fixture else 0.0
    following_rating = safe_float(following_fixture.get("current_rating"), 0.0) if following_fixture else 0.0

    row: dict[str, Any] = {
        "Name": name,
        "Short Name": short_name,
        "Player ID": raw.get("playerId"),
        "Opta Player ID": raw.get("providerId"),
        "Club": raw.get("teamAcronymName") or raw.get("teamShortName"),
        "Club Name": raw.get("teamOfficialName") or raw.get("teamShortName"),
        "Team ID": raw.get("teamId"),
        "Competition ID": raw.get("competitionId"),
        "Position": position,
        "Value": value,
        "Nationality": "",
        "News": availability_text(raw.get("availabilityStatus")),
        "Availability Status": raw.get("availabilityStatus"),
        "Is Active": bool(raw.get("isActive")),
        "Is Playing": bool(raw.get("isPlaying")),
        "Visionary": False,
        "Total Points": safe_int(total_points),
        "Previous Season Points": safe_int(prior_points),
        "Selected Percentage": selected,
        "Selected Percentage Change 1W": 0.0,
        "Selected Percentage Change Since Last Global Price Change": 0.0,
        "Recommendation": "Monitor",
        "Hot Pick": False,
        "Total Games Played": 0,
        "Total Over 4 Gameweeks": safe_int(raw.get("lastMDPoints")),
        "Form Rating": 0,
        "Raw Form": raw_form,
        "Average Points": avg_points,
        "Games Played Over 4 Gameweeks": 0,
        "Points Per Game Over 4 Gameweeks": 0.0,
        "Points Per Million": round(total_points / value, 2) if value else 0.0,
        "Points Per Million Over 4 Gameweeks": round(safe_float(raw.get("lastMDPoints")) / value, 2) if value else 0.0,
        "Previous Season Points Per Million": round(prior_points / value, 2) if value else 0.0,
        "Total Goals": 0,
        "Total Assists": 0,
        "Total Goals + Assists": 0,
        "Total Red Cards": 0,
        "Total Yellow Cards": 0,
        "Total Saves": 0,
        "Total Own Goals": 0,
        "Total Conceeded": 0,
        "Total Conceded": 0,
        "Total Clean Sheet": 0,
        "Total Bonus Points": safe_int(raw.get("bonusPointsWon")),
        "Total Bonus Games": 0,
        "Total Missed Penalties": 0,
        "Total Clearances": 0,
        "Total 1 min Appearances": 0,
        "Total 60 min Appearances": 0,
        "Transfers In": safe_int(raw.get("transferIn")),
        "Transfers Out": safe_int(raw.get("transferOut")),
        "upcoming_fixtures": upcoming,
        "Next Fixture Rating": round(next_rating, 1),
        "Next Fixture Score": fixture_rating_to_score(next_rating) if next_fixture else "-",
        "Next Fixture Details": fixture_details_text(upcoming[:1], position),
        "Following Fixture Rating": round(following_rating, 1),
        "Following Fixture Score": fixture_rating_to_score(following_rating) if following_fixture else "-",
        "Following Fixture Details": fixture_details_text(upcoming[1:2], position),
        "Next Three Fixture Rating": round(
            sum(safe_float(f.get("current_rating")) for f in upcoming[:3]) / len(upcoming[:3]), 1
        ) if upcoming[:3] else 0.0,
        "Next Three Fixture Details": fixture_details_text(upcoming[:3], position),
        "Decision Rating": 0.0,
    }

    # Fill matchday columns. The WSL preseason feed has upcoming fixtures but not
    # completed match-by-match scoring yet, so these start as '-' until richer
    # matchweek scoring data appears.
    for i in range(1, MAX_MATCHDAY_COLUMNS + 1):
        row[str(i)] = "-"

    # If a future feed begins exposing a points-per-match list, support a few
    # likely key names without changing downstream data shape.
    for match in raw.get("matchPoints", []) or raw.get("matchwisePoints", []) or []:
        md = str(match.get("matchdayId") or match.get("gamedayId") or "")
        if md and md in row:
            pts = safe_int(match.get("points") or match.get("totalPoints"))
            row[md] = {"points": pts, "base_points": pts, "visionary_bonus": 0, "tooltip": "WSL feed match points"}

    row["Form Rating"] = form_rating(row)
    row["Recommendation"] = recommendation(row)
    row["Decision Rating"] = decision_rating(row)
    row["Hot Pick"] = bool(row["Decision Rating"] >= 75 and selected < 25)
    return row


def availability_text(status: Any) -> str | None:
    # The feed currently uses numeric availabilityStatus. Keep the raw value too.
    if status in (None, ""):
        return None
    mapping = {
        1: None,  # appears available in the launch feed
        0: "Unavailable or inactive",
        2: "Doubtful / check status",
        3: "Unavailable / check status",
    }
    return mapping.get(status, f"Availability status: {status}")


def build_outputs(from_local: bool = False) -> dict[str, Any]:
    feeds = {
        key: fetch_json(path, cache_name=f"{key}.json", from_local=from_local)
        for key, path in URLS.items()
    }

    players_raw = value_of(feeds["players"]) or []
    fixtures_raw = value_of(feeds["fixtures"]) or []
    teams_value = value_of(feeds["teams"]) or {}
    teams_raw = teams_value.get("teams", []) if isinstance(teams_value, dict) else []

    players = [transform_player(p) for p in players_raw]
    history = update_history(players)
    last_price_change = detect_last_global_price_change_date(history)

    for player in players:
        selected = safe_float(player.get("Selected Percentage"))
        player["Selected Percentage Change 1W"] = selected_delta(history, player["Name"], selected, days_back=7)
        player["Selected Percentage Change Since Last Global Price Change"] = selected_delta_since(
            history, player["Name"], selected, last_price_change
        )

    fixtures = [normalize_fixture(f) for f in fixtures_raw]
    teams = [normalize_team(t) for t in teams_raw]

    metadata = {
        "source": "WSL Fantasy public JSON feeds used by create-team UI",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "matchday_id": MATCHDAY_ID,
        "tour_id": TOUR_ID,
        "player_count": len(players),
        "fixture_count": len(fixtures),
        "team_count": len(teams),
        "last_global_price_change_date": last_price_change,
        "feed_urls": {key: urljoin(BASE_URL, path) for key, path in URLS.items()},
        "fixture_model": {
            "version": "wsl-feed-current-rating-v1",
            "note": "Uses WSL feed currentRating. A WSL xG/team-strength model can be added later.",
        },
        "decision_rating": {
            "version": "wsl-v1-position-specific",
            "weights": {
                "GK": {"fixture": 0.85, "form": 0.15},
                "DEF": {"fixture": 0.65, "form": 0.35},
                "MID": {"fixture": 0.35, "form": 0.65},
                "FOR": {"fixture": 0.25, "form": 0.75},
            },
            "uses": ["Form Rating", "Next Fixture Rating"],
        },
    }

    output = {"metadata": metadata, "players": players}
    TRANSFORMED_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    FIXTURES_PATH.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2), encoding="utf-8")
    TEAMS_PATH.write_text(json.dumps(teams, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build WSL Fantasy transformed_data.json")
    parser.add_argument(
        "--from-local",
        action="store_true",
        help="Read raw_feeds/*.json instead of downloading fresh copies.",
    )
    args = parser.parse_args()
    output = build_outputs(from_local=args.from_local)
    print(
        f"Wrote {TRANSFORMED_PATH.name} with {len(output['players'])} players; "
        f"metadata: {output['metadata']['fixture_count']} fixtures, {output['metadata']['team_count']} teams."
    )


if __name__ == "__main__":
    main()
