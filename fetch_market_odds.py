"""Fetch Pinnacle WSL team-total odds through OddsPapi and derive clean-sheet probabilities.

The script intentionally uses one OddsPapi billable request per run. Team names are
resolved by matching OddsPapi fixture kickoffs to the official WSL fixture feed,
so no second participant lookup is required.

Output: market_odds.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "market_odds.json"
ODDSPAPI_BASE = "https://api.oddspapi.io/v4"
WSL_TOURNAMENT_ID = 1044
WSL_FIXTURES_URL = "https://gaming.wslfootball.com/feeds/fixtures/fixtures_en_1.json?v=3"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def load_official_fixtures() -> list[dict[str, Any]]:
    response = requests.get(
        WSL_FIXTURES_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 WSL fantasy market odds fetcher",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.wslfootball.com/fantasy/create-team",
        },
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("Data", {}).get("Value", []) or []


def flatten_odds_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept OddsPapi's list response and a few plausible wrapped shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "fixtures", "odds", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # Sometimes a dictionary may be keyed by fixture id.
        values = [v for v in payload.values() if isinstance(v, dict) and ("fixtureId" in v or "bookmakerOdds" in v)]
        if values:
            return values
    return []


def extract_price(player_entry: Any) -> tuple[float | None, bool]:
    if not isinstance(player_entry, dict):
        return None, False
    # OddsPapi commonly nests the price under players -> "0".
    candidates = []
    if "players" in player_entry and isinstance(player_entry["players"], dict):
        candidates.extend(player_entry["players"].values())
    else:
        candidates.append(player_entry)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        price = candidate.get("price")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        active = bool(candidate.get("active", True))
        if price_f > 1.0:
            return price_f, active
    return None, False


def find_team_total_half(markets: dict[str, Any], side: str) -> dict[str, Any] | None:
    """Find an active team-total O/U 0.5 pair for home or away."""
    over: tuple[float, bool, bool] | None = None  # price, player active, main line
    under: tuple[float, bool, bool] | None = None

    for market in (markets or {}).values():
        if not isinstance(market, dict) or not market.get("marketActive", True):
            continue
        market_id = str(market.get("bookmakerMarketId") or "")
        if "teamTotal" not in market_id:
            continue
        market_main = False
        for outcome in (market.get("outcomes") or {}).values():
            if not isinstance(outcome, dict):
                continue
            for player in (outcome.get("players") or {}).values():
                if not isinstance(player, dict):
                    continue
                outcome_id = str(player.get("bookmakerOutcomeId") or "").lower()
                if outcome_id not in {f"{side}/0.5/over", f"{side}/0.5/under"}:
                    continue
                try:
                    price = float(player.get("price"))
                except (TypeError, ValueError):
                    continue
                active = bool(player.get("active", True))
                main_line = bool(player.get("mainLine", False))
                market_main = market_main or main_line
                target = (price, active, main_line)
                if outcome_id.endswith("/over"):
                    # Prefer active, then main-line entries if duplicates exist.
                    if over is None or (active, main_line) > (over[1], over[2]):
                        over = target
                else:
                    if under is None or (active, main_line) > (under[1], under[2]):
                        under = target

    if not over or not under or not over[1] or not under[1]:
        return None

    over_odds, _, _ = over
    under_odds, _, _ = under
    over_raw = 1.0 / over_odds
    under_raw = 1.0 / under_odds
    denom = over_raw + under_raw
    if denom <= 0:
        return None

    fair_under = under_raw / denom
    return {
        "over_odds": round(over_odds, 4),
        "under_odds": round(under_odds, 4),
        "raw_under_probability": round(under_raw, 6),
        "overround": round(denom - 1.0, 6),
        "fair_under_probability": round(fair_under, 6),
        "main_line": bool(over[2] or under[2]),
    }


def match_official_fixture(odd_fixture: dict[str, Any], official: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = parse_dt(odd_fixture.get("startTime"))
    if not target:
        return None

    matches: list[tuple[float, dict[str, Any]]] = []
    for fixture in official:
        dt = parse_dt(fixture.get("matchDateTimeUtc"))
        if not dt:
            continue
        delta = abs((dt - target).total_seconds())
        if delta <= 10 * 60:  # tolerate small feed discrepancies
            matches.append((delta, fixture))

    if not matches:
        return None
    matches.sort(key=lambda x: x[0])
    # Kickoffs are unique enough for current WSL schedule; reject an ambiguous tie.
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][1]


def main() -> int:
    api_key = os.getenv("ODDSPAPI_API_KEY", "").strip()
    if not api_key:
        print("ODDSPAPI_API_KEY is not set; leaving market_odds.json unchanged if present.")
        return 0

    try:
        official = load_official_fixtures()
    except Exception as exc:
        print(f"Warning: could not load official WSL fixtures: {exc}")
        return 0

    url = f"{ODDSPAPI_BASE}/odds-by-tournaments"
    params = {
        "bookmaker": "pinnacle",
        "tournamentIds": str(WSL_TOURNAMENT_ID),
        "apiKey": api_key,
    }

    try:
        response = requests.get(url, params=params, timeout=45)
        if response.status_code >= 400:
            print(f"Warning: OddsPapi returned HTTP {response.status_code}: {response.text[:500]}")
            return 0
        payload = response.json()
    except Exception as exc:
        print(f"Warning: OddsPapi fetch failed: {exc}")
        return 0

    odds_fixtures = flatten_odds_payload(payload)
    output_rows: list[dict[str, Any]] = []
    unmatched = 0

    for odd_fixture in odds_fixtures:
        pinnacle = (odd_fixture.get("bookmakerOdds") or {}).get("pinnacle") or {}
        if not pinnacle or pinnacle.get("suspended") is True:
            continue
        markets = pinnacle.get("markets") or {}
        home_tt = find_team_total_half(markets, "home")
        away_tt = find_team_total_half(markets, "away")
        if not home_tt and not away_tt:
            continue

        fixture = match_official_fixture(odd_fixture, official)
        if not fixture:
            unmatched += 1
            continue

        home_code = fixture.get("homeAcronymName") or ""
        away_code = fixture.get("awayAcronymName") or ""
        # A team's clean sheet is the opponent's probability of scoring under 0.5.
        home_cs = away_tt["fair_under_probability"] if away_tt else None
        away_cs = home_tt["fair_under_probability"] if home_tt else None

        output_rows.append({
            "match_id": fixture.get("matchId"),
            "fixture_id": odd_fixture.get("fixtureId"),
            "pinnacle_fixture_id": pinnacle.get("bookmakerFixtureId"),
            "start_time_utc": odd_fixture.get("startTime"),
            "home_team": home_code,
            "away_team": away_code,
            "home_name": fixture.get("homeOfficialName") or fixture.get("homeMediaName"),
            "away_name": fixture.get("awayOfficialName") or fixture.get("awayMediaName"),
            "home_market_cs_probability": home_cs,
            "away_market_cs_probability": away_cs,
            "home_opponent_team_total_0_5": away_tt,
            "away_opponent_team_total_0_5": home_tt,
            "source": "Pinnacle",
            "method": "opponent_team_total_under_0.5_devig",
            "market_updated_at": odd_fixture.get("updatedAt"),
        })

    result = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "Pinnacle via OddsPapi",
            "method": "Opponent team total Under 0.5, de-vigged against Over 0.5",
            "tournament_id": WSL_TOURNAMENT_ID,
            "fixture_count": len(output_rows),
            "unmatched_fixture_count": unmatched,
            "note": "A team's CS probability equals the opponent's fair probability of scoring 0 goals. Prices are normalized within the same Pinnacle O/U 0.5 market to remove bookmaker margin.",
        },
        "fixtures": output_rows,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.name}: {len(output_rows)} WSL fixtures with usable Pinnacle CS markets ({unmatched} unmatched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
