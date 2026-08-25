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
from zoneinfo import ZoneInfo

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
UK_TZ = ZoneInfo("Europe/London")

WSL_COMPETITION_ID = "wpll::Football_Competition::e32284e8a1214f1ca83a3245d690b336"
WSL2_COMPETITION_ID = "wpll::Football_Competition::422757a2c70d450eba118ad97bed5222"
COMPETITION_LABELS = {
    WSL_COMPETITION_ID: "WSL",
    WSL2_COMPETITION_ID: "WSL2",
}


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


def fixture_local_dt(value: str | None) -> datetime | None:
    dt = parse_dt(value)
    return dt.astimezone(UK_TZ) if dt else None


def format_fixture_date(value: str | None) -> str:
    dt = fixture_local_dt(value)
    if not dt:
        return ""
    # WSL fixtures are shown in UK local time (GMT/BST as appropriate).
    return f"{dt.day} {dt.strftime('%b')}"


def format_fixture_time(value: str | None) -> str:
    dt = fixture_local_dt(value)
    if not dt:
        return ""
    return dt.strftime("%H:%M")


def format_fixture_date_iso(value: str | None) -> str:
    dt = fixture_local_dt(value)
    if not dt:
        return ""
    return dt.date().isoformat()


def format_fixture_day(value: str | None) -> str:
    dt = fixture_local_dt(value)
    if not dt:
        return ""
    return dt.strftime("%A")


def format_fixture_day_short(value: str | None) -> str:
    dt = fixture_local_dt(value)
    if not dt:
        return ""
    return dt.strftime("%a").upper()


def normalize_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "match_id": fixture.get("matchId"),
        "provider_id": fixture.get("providerId"),
        "competition_id": fixture.get("competitionId"),
        "status": fixture.get("status"),
        "provider_status": fixture.get("providerStatus"),
        "match_date_time_utc": fixture.get("matchDateTimeUtc"),
        "fixture_date_iso": format_fixture_date_iso(fixture.get("matchDateTimeUtc")),
        "fixture_day": format_fixture_day(fixture.get("matchDateTimeUtc")),
        "fixture_day_short": format_fixture_day_short(fixture.get("matchDateTimeUtc")),
        "fixture_day_number": fixture_local_dt(fixture.get("matchDateTimeUtc")).isoweekday() if fixture_local_dt(fixture.get("matchDateTimeUtc")) else None,
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


def competition_label(competition_id: str | None) -> str:
    return COMPETITION_LABELS.get(competition_id, "Other")


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def build_competition_fixture_calibration(players_raw: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Create league-relative fixture difficulty bands.

    The launch feed's currentRating values live in very different numeric bands
    for WSL and WSL2. Treating them as one global scale makes virtually every
    WSL2 fixture look easier than every WSL fixture. For fantasy purposes we
    instead compare an opponent to the other opponents in the same competition.

    We deduplicate by competition/team/match, then use the 10th and 90th
    percentiles of source difficulty as the easy/hard anchors. Those anchors
    map to 90 and 10 on the parser's common opportunity scale.
    """
    by_comp: dict[str, dict[tuple[str, str], float]] = {}
    for player in players_raw:
        comp = player.get("competitionId")
        own_team = str(player.get("teamId") or player.get("teamAcronymName") or "")
        if not comp:
            continue
        bucket = by_comp.setdefault(comp, {})
        for f in player.get("upcomingFixtures", []) or []:
            raw = f.get("currentRating")
            if raw in (None, ""):
                continue
            key = (own_team, str(f.get("matchId") or f.get("matchdayId") or len(bucket)))
            bucket[key] = safe_float(raw)

    calibration: dict[str, dict[str, float]] = {}
    for comp, keyed in by_comp.items():
        vals = list(keyed.values())
        easy = percentile(vals, 0.10)
        hard = percentile(vals, 0.90)
        if easy is None or hard is None:
            continue
        if hard <= easy:
            easy = min(vals)
            hard = max(vals)
        if hard <= easy:
            hard = easy + 1.0
        calibration[comp] = {
            "easy_anchor": round(easy, 2),
            "hard_anchor": round(hard, 2),
            "sample_count": float(len(vals)),
        }
    return calibration


def wsl_difficulty_to_opportunity(
    difficulty: float | None,
    competition_id: str | None = None,
    calibration: dict[str, dict[str, float]] | None = None,
) -> float | None:
    """Convert source difficulty into a common 0-100 league-relative opportunity score.

    Higher source currentRating = harder. Higher parser Fixture Rating = better.
    WSL and WSL2 are normalized separately so a mid-table WSL2 opponent is not
    automatically rated easier than every WSL opponent merely because the two
    competitions occupy different source-rating bands.
    """
    if difficulty is None:
        return None
    d = safe_float(difficulty)
    band = (calibration or {}).get(competition_id or "")
    if band:
        easy = safe_float(band.get("easy_anchor"))
        hard = safe_float(band.get("hard_anchor"))
        span = max(hard - easy, 0.01)
        # easy anchor -> 90 opportunity, hard anchor -> 10; allow modest
        # extension beyond anchors and clamp to the common 0-100 scale.
        opportunity = 90.0 - ((d - easy) / span) * 80.0
        return round(max(0.0, min(100.0, opportunity)), 1)

    # Conservative fallback if a new/unknown competition appears.
    return round(max(0.0, min(100.0, 190.0 - (2.0 * d))), 1)

def fixture_rating_to_score(rating: float | None) -> int | str:
    """Convert a 0-100 opportunity rating to the viewer's legacy 1-5 bucket.

    This matches the NWSL convention: 1 = elite/easiest, 5 = very difficult.
    """
    if rating is None:
        return "-"
    r = safe_float(rating, 50.0)
    if r >= 80:
        return 1
    if r >= 65:
        return 2
    if r >= 45:
        return 3
    if r >= 25:
        return 4
    return 5


def build_upcoming_fixture(
    raw: dict[str, Any],
    own_team_id: str | None = None,
    own_team_name: str | None = None,
    own_team_short_name: str | None = None,
    competition_id: str | None = None,
    fixture_calibration: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    difficulty = raw.get("currentRating")
    opportunity = wsl_difficulty_to_opportunity(difficulty, competition_id, fixture_calibration)
    location = raw.get("location")
    opponent_id = raw.get("vsTeamAcronymName") or compact_id(raw.get("vsTeamId"))
    opponent_name = raw.get("vsTeamName")
    opponent_short = raw.get("vsTeamShortName")

    if location == "H":
        home_id, home_name, home_short = own_team_id, own_team_name, own_team_short_name
        away_id, away_name, away_short = opponent_id, opponent_name, opponent_short
    elif location == "A":
        home_id, home_name, home_short = opponent_id, opponent_name, opponent_short
        away_id, away_name, away_short = own_team_id, own_team_name, own_team_short_name
    else:
        home_id = home_name = home_short = ""
        away_id = away_name = away_short = ""

    fixture_dt = fixture_local_dt(raw.get("matchDateTimeUtc"))

    return {
        "match_date_time_utc": raw.get("matchDateTimeUtc"),
        "fixture_date_iso": format_fixture_date_iso(raw.get("matchDateTimeUtc")),
        "fixture_day": format_fixture_day(raw.get("matchDateTimeUtc")),
        "fixture_day_short": format_fixture_day_short(raw.get("matchDateTimeUtc")),
        "fixture_day_number": fixture_dt.isoweekday() if fixture_dt else None,
        "game_date": format_fixture_date(raw.get("matchDateTimeUtc")),
        "kick_off_time": format_fixture_time(raw.get("matchDateTimeUtc")),
        "game_week": str(raw.get("matchdayId") or ""),
        "match_id": raw.get("matchId"),
        "opponent_id": opponent_id,
        "opponent_name": opponent_name,
        "opponent_short_name": opponent_short,
        "location": location,
        # Preserve the source field for debugging/reference. Higher = harder.
        "current_rating": difficulty,
        "fixture_difficulty": difficulty,
        "competition_id": competition_id,
        "league": competition_label(competition_id),
        # Normalized parser contract. Higher = better.
        "opportunity_rating": opportunity,
        "home_id": home_id or "",
        "home_name": home_name or "",
        "home_short_name": home_short or "",
        "away_id": away_id or "",
        "away_name": away_name or "",
        "away_short_name": away_short or "",
    }


def fixture_details_text(fixtures: list[dict[str, Any]], position: str) -> str:
    if not fixtures:
        return "No upcoming fixture in feed."
    lines = ["Upcoming fixtures from WSL feed:"]
    opportunities: list[float] = []
    for f in fixtures:
        opponent = f.get("opponent_id") or f.get("opponent_short_name") or f.get("opponent_name") or "TBD"
        loc = f.get("location") or ""
        difficulty = f.get("fixture_difficulty", f.get("current_rating"))
        opportunity = f.get("opportunity_rating")
        if opportunity is None:
            opportunity = wsl_difficulty_to_opportunity(difficulty, f.get("competition_id"))
        score = fixture_rating_to_score(opportunity)
        lines.append(
            f"GW{f.get('game_week')}: vs {opponent} ({loc}) on {f.get('game_date')} {f.get('kick_off_time')}"
        )
        if difficulty is not None:
            lines.append(f"  WSL source difficulty: {difficulty}/100 (higher = harder)")
        if opportunity is not None:
            opportunities.append(float(opportunity))
            lines.append(f"  {f.get('league') or 'League'}-relative fixture opportunity: {opportunity}/100 (higher = better); table score: {score}/5")
        if position in {"GK", "DEF"}:
            lines.append("  Temporary defensive opportunity is normalized within the player's competition until a WSL xG model is added.")
        else:
            lines.append("  Temporary attacking opportunity is normalized within the player's competition until a WSL xG model is added.")
    if opportunities:
        lines.append(f"Average fixture opportunity: {round(sum(opportunities) / len(opportunities), 1)}/100")
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


def transform_player(raw: dict[str, Any], fixture_calibration: dict[str, dict[str, float]]) -> dict[str, Any]:
    name = f"{raw.get('mediaFirstName', '').strip()} {raw.get('mediaLastName', '').strip()}".strip()
    short_name = raw.get("mediaShortName") or name
    position = POSITION_MAP.get(str(raw.get("skillName") or "").lower(), str(raw.get("skillName") or "").upper())
    value = safe_float(raw.get("valuation"))
    selected = safe_float(raw.get("selectedPercentage"))
    total_points = safe_float(raw.get("totalPoints"))
    prior_points = safe_float(raw.get("pointsLastSeason"))
    avg_points = raw.get("averagePoints")
    raw_form = raw.get("form")
    own_team_id = raw.get("teamAcronymName") or raw.get("teamShortName")
    own_team_name = raw.get("teamOfficialName") or raw.get("teamShortName")
    own_team_short_name = raw.get("teamShortName") or own_team_name
    competition_id = raw.get("competitionId")
    upcoming = [
        build_upcoming_fixture(
            f, own_team_id, own_team_name, own_team_short_name,
            competition_id, fixture_calibration
        )
        for f in raw.get("upcomingFixtures", [])
    ]
    next_fixture = upcoming[0] if upcoming else None
    following_fixture = upcoming[1] if len(upcoming) > 1 else None
    next_rating = safe_float(next_fixture.get("opportunity_rating"), 0.0) if next_fixture else 0.0
    following_rating = safe_float(following_fixture.get("opportunity_rating"), 0.0) if following_fixture else 0.0

    row: dict[str, Any] = {
        "Name": name,
        "Short Name": short_name,
        "Player ID": raw.get("playerId"),
        "Opta Player ID": raw.get("providerId"),
        "Club": raw.get("teamAcronymName") or raw.get("teamShortName"),
        "Club Name": raw.get("teamOfficialName") or raw.get("teamShortName"),
        "Team ID": raw.get("teamId"),
        "Competition ID": competition_id,
        "League": competition_label(competition_id),
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
        "Next Fixture Day": next_fixture.get("fixture_day") if next_fixture else "",
        "Next Fixture Day Short": next_fixture.get("fixture_day_short") if next_fixture else "",
        "Next Fixture Day Number": next_fixture.get("fixture_day_number") if next_fixture else None,
        "Next Fixture Date ISO": next_fixture.get("fixture_date_iso") if next_fixture else "",
        "Next Fixture Kickoff": next_fixture.get("kick_off_time") if next_fixture else "",
        "Next Fixture Opponent": next_fixture.get("opponent_id") if next_fixture else "",
        "Next Fixture H/A": next_fixture.get("location") if next_fixture else "",
        "Sub Flex": max(0, (next_fixture.get("fixture_day_number") or 5) - 5) if next_fixture else 0,
        "Next Fixture Rating": round(next_rating, 1),
        "Next Fixture Score": fixture_rating_to_score(next_rating) if next_fixture else "-",
        "Next Fixture Details": fixture_details_text(upcoming[:1], position),
        "Following Fixture Rating": round(following_rating, 1),
        "Following Fixture Score": fixture_rating_to_score(following_rating) if following_fixture else "-",
        "Following Fixture Details": fixture_details_text(upcoming[1:2], position),
        "Next Three Fixture Rating": round(
            sum(safe_float(f.get("opportunity_rating")) for f in upcoming[:3]) / len(upcoming[:3]), 1
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

    fixture_calibration = build_competition_fixture_calibration(players_raw)
    players = [transform_player(p, fixture_calibration) for p in players_raw]
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
            "version": "wsl-competition-relative-difficulty-v3",
            "note": "WSL currentRating is higher-is-harder, but WSL and WSL2 occupy different source bands. Ratings are normalized separately within each competition using the 10th/90th percentile source difficulties as 90/10 opportunity anchors. Higher Fixture Rating = better. A WSL xG/team-strength model can replace this later.",
            "competition_calibration": {
                competition_label(comp): {**vals, "competition_id": comp}
                for comp, vals in fixture_calibration.items()
            },
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
