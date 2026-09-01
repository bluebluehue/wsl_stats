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
GK_MODEL_INPUTS_PATH = ROOT / "gk_model_inputs.json"
MARKET_ODDS_PATH = ROOT / "market_odds.json"
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


# 2026/27 cross-division transition layer.
PROMOTED_TO_WSL = {"BIR", "CRY", "CHA"}
RELEGATED_TO_WSL2 = {"LEI"}

# Preseason transition profiles. These deliberately distinguish the three
# promoted clubs rather than treating "promoted" as one generic team type.
# Factors >1 on defense mean more goals conceded; <1 on attack means weaker
# attacking output after stepping up a division.
PROMOTED_WSL_PROFILES = {
    # 2025/26 WSL2 champions; meaningful WSL-experience added.
    "BIR": {"strength_band": (0.30, 0.44), "defense_bridge": 1.12, "attack_bridge": 0.90},
    # 2025/26 runners-up; recent WSL experience plus substantial recruitment.
    "CRY": {"strength_band": (0.28, 0.42), "defense_bridge": 1.14, "attack_bridge": 0.89},
    # 2025/26 third/play-off winner; excellent WSL2 defense, but weakest
    # promoted baseline and the largest step-up penalty of the three.
    "CHA": {"strength_band": (0.20, 0.32), "defense_bridge": 1.22, "attack_bridge": 0.82},
}

RELEGATED_WSL2_PROFILES = {
    # Freshly relegated WSL side retaining substantial top-flight quality.
    # Start in the elite WSL2 band rather than merely "above average".
    "LEI": {"strength_band": (0.88, 0.95), "defense_bridge": 0.78, "attack_bridge": 1.24},
}

# Fixture Model v5. The WSL source rating remains useful, but it is now only
# one input. The independent team-matchup prior gets the larger weight.
SOURCE_RATING_WEIGHT = 0.35
TEAM_MATCHUP_WEIGHT = 0.65
HOME_ADVANTAGE_POINTS = 4.0
AWAY_DISADVANTAGE_POINTS = -4.0


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



def assign_canonical_fantasy_gameweeks(
    fixtures: list[dict[str, Any]],
    max_gap_days: int = 3,
) -> list[dict[str, Any]]:
    """Assign a shared Fantasy GW across WSL and WSL2 by calendar window.

    The two competitions' own matchday numbers diverge during the season.
    Fantasy rotation/substitution analysis needs a single chronological
    scoring-window index instead.

    We cluster published *league* fixtures into calendar windows. Consecutive
    fixture dates no more than max_gap_days apart belong to the same Fantasy
    GW. A later cluster becomes the next Fantasy GW. This naturally preserves
    WSL-only or WSL2-only league weekends as their own fantasy scoring window.

    The original competition matchday remains available as league_game_week.
    """
    dated = []
    for fixture in fixtures:
        date_str = fixture.get("fixture_date_iso")
        if not date_str:
            continue
        try:
            d = datetime.fromisoformat(date_str).date()
        except Exception:
            continue
        dated.append((d, fixture))

    dated.sort(key=lambda item: (item[0], str(item[1].get("match_id") or "")))

    clusters: list[dict[str, Any]] = []
    for d, fixture in dated:
        if not clusters:
            clusters.append({"start": d, "end": d, "fixtures": [fixture]})
            continue

        previous_end = clusters[-1]["end"]
        gap = (d - previous_end).days

        if gap <= max_gap_days:
            clusters[-1]["end"] = max(clusters[-1]["end"], d)
            clusters[-1]["fixtures"].append(fixture)
        else:
            clusters.append({"start": d, "end": d, "fixtures": [fixture]})

    windows: list[dict[str, Any]] = []
    for fantasy_gw, cluster in enumerate(clusters, start=1):
        active_competitions = sorted({
            str(f.get("competition_id") or "")
            for f in cluster["fixtures"]
            if f.get("competition_id")
        })
        active_leagues = [competition_label(comp) for comp in active_competitions]

        for fixture in cluster["fixtures"]:
            fixture["league_game_week"] = fixture.get("game_week")
            fixture["fantasy_game_week"] = str(fantasy_gw)
            fixture["fantasy_window_start"] = cluster["start"].isoformat()
            fixture["fantasy_window_end"] = cluster["end"].isoformat()

        if len(active_leagues) == 1:
            window_type = f"{active_leagues[0]}_ONLY"
            free_hit_eligible = True
        else:
            window_type = "BOTH_LEAGUES"
            free_hit_eligible = False

        windows.append({
            "fantasy_game_week": str(fantasy_gw),
            "start_date": cluster["start"].isoformat(),
            "end_date": cluster["end"].isoformat(),
            "active_competition_ids": active_competitions,
            "active_leagues": active_leagues,
            "window_type": window_type,
            "free_hit_eligible": free_hit_eligible,
            "fixture_count": len(cluster["fixtures"]),
        })

    return windows




def load_gk_model_inputs() -> dict[str, Any]:
    if not GK_MODEL_INPUTS_PATH.exists():
        return {"teams": {}, "keepers": {}, "manual_team_adjustments": {}, "manual_keeper_adjustments": {}}
    try:
        return json.loads(GK_MODEL_INPUTS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not load {GK_MODEL_INPUTS_PATH.name}: {exc}")
        return {"teams": {}, "keepers": {}, "manual_team_adjustments": {}, "manual_keeper_adjustments": {}}



def load_market_odds() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load de-vigged Pinnacle clean-sheet probabilities keyed by official WSL match id."""
    if not MARKET_ODDS_PATH.exists():
        return {}, {}
    try:
        payload = json.loads(MARKET_ODDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: could not load {MARKET_ODDS_PATH.name}: {exc}")
        return {}, {}

    if isinstance(payload, list):
        fixtures = payload
        metadata = {}
    else:
        fixtures = payload.get("fixtures", []) or []
        metadata = payload.get("metadata", {}) or {}

    lookup = {str(row.get("match_id")): row for row in fixtures if row.get("match_id")}
    return metadata, lookup


def market_fields_for_fixture(
    match_id: str | None,
    location: str | None,
    market_lookup: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    row = (market_lookup or {}).get(str(match_id or ""))
    if not row:
        return {
            "market_cs_probability": None,
            "market_cs_source": None,
            "market_cs_method": None,
            "market_cs_updated_at": None,
            "market_cs_over_odds": None,
            "market_cs_under_odds": None,
        }

    if location == "H":
        probability = row.get("home_market_cs_probability")
        raw = row.get("home_opponent_team_total_0_5") or {}
    elif location == "A":
        probability = row.get("away_market_cs_probability")
        raw = row.get("away_opponent_team_total_0_5") or {}
    else:
        probability = None
        raw = {}

    return {
        "market_cs_probability": probability,
        "market_cs_source": row.get("source"),
        "market_cs_method": row.get("method"),
        "market_cs_updated_at": row.get("market_updated_at"),
        "market_cs_over_odds": raw.get("over_odds"),
        "market_cs_under_odds": raw.get("under_odds"),
    }

def _valid_number(value: Any) -> bool:
    return value is not None and value != ""


def _average(values: list[float], default: float) -> float:
    clean = [safe_float(v) for v in values if _valid_number(v)]
    return (sum(clean) / len(clean)) if clean else default


def poisson_prob_at_least(mean: float, threshold: int) -> float:
    """P(X >= threshold) for a Poisson random variable."""
    mean = max(0.0, safe_float(mean))
    if threshold <= 0:
        return 1.0
    cumulative = 0.0
    for k in range(threshold):
        cumulative += math.exp(-mean) * (mean ** k) / math.factorial(k)
    return max(0.0, min(1.0, 1.0 - cumulative))


TEAM_CODE_ALIASES = {
    "MNU": "MUN",
}

def canonical_gk_team_code(code: str | None) -> str:
    raw = str(code or "").upper()
    return TEAM_CODE_ALIASES.get(raw, raw)


def build_gk_team_priors(
    model_inputs: dict[str, Any],
    team_strength: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    teams = model_inputs.get("teams", {}) or {}
    manual = model_inputs.get("manual_team_adjustments", {}) or {}
    team_strength = team_strength or {}

    by_league: dict[str, list[dict[str, Any]]] = {}
    for code, row in teams.items():
        league = row.get("prior_league")
        mp = max(1.0, safe_float(row.get("mp"), 22))
        if league not in {"WSL", "WSL2"}:
            continue
        if not (_valid_number(row.get("gf")) and _valid_number(row.get("ga"))):
            continue
        by_league.setdefault(league, []).append({
            "code": code,
            "ga90": safe_float(row.get("ga")) / mp,
            "gf90": safe_float(row.get("gf")) / mp,
            "sot": safe_float(row.get("sot_for_per90")) if _valid_number(row.get("sot_for_per90")) else None,
            "cs_rate": safe_float(row.get("clean_sheets")) / mp if _valid_number(row.get("clean_sheets")) else None,
            "saves": safe_float(row.get("saves_per_game")) if _valid_number(row.get("saves_per_game")) else None,
            "big_chances90": safe_float(row.get("big_chances")) / mp if _valid_number(row.get("big_chances")) else None,
        })

    baselines: dict[str, dict[str, float]] = {}
    for league, rows in by_league.items():
        baselines[league] = {
            "ga90": _average([r["ga90"] for r in rows], 1.45),
            "gf90": _average([r["gf90"] for r in rows], 1.45),
            "sot": _average([r["sot"] for r in rows if r["sot"] is not None], 4.5),
            "cs_rate": _average([r["cs_rate"] for r in rows if r["cs_rate"] is not None], 0.25),
            "saves": _average([r["saves"] for r in rows if r["saves"] is not None], 2.8),
            "big_chances90": _average([r["big_chances90"] for r in rows if r["big_chances90"] is not None], 1.5),
        }

    out: dict[str, dict[str, Any]] = {}

    for code, row in teams.items():
        prior_league = row.get("prior_league") or "WSL"
        target_league = "WSL" if row.get("promoted") else "WSL2" if row.get("relegated") else prior_league
        base = baselines.get(prior_league, baselines.get(target_league, {
            "ga90": 1.45, "gf90": 1.45, "sot": 4.5, "cs_rate": 0.25,
            "saves": 2.8, "big_chances90": 1.5,
        }))
        target_base = baselines.get(target_league, base)
        mp = max(1.0, safe_float(row.get("mp"), 22))

        has_history = _valid_number(row.get("gf")) and _valid_number(row.get("ga"))
        if has_history:
            ga90 = safe_float(row.get("ga")) / mp
            gf90 = safe_float(row.get("gf")) / mp
            sot = safe_float(row.get("sot_for_per90")) if _valid_number(row.get("sot_for_per90")) else None
            cs_rate = safe_float(row.get("clean_sheets")) / mp if _valid_number(row.get("clean_sheets")) else None
            saves_pg = safe_float(row.get("saves_per_game")) if _valid_number(row.get("saves_per_game")) else None
            big90 = safe_float(row.get("big_chances")) / mp if _valid_number(row.get("big_chances")) else None

            ga_component = ga90 / max(base["ga90"], 0.01)
            if cs_rate is not None:
                cs_concede_component = (1.0 - cs_rate) / max(1.0 - base["cs_rate"], 0.05)
                defense_factor = 0.70 * ga_component + 0.30 * cs_concede_component
            else:
                defense_factor = ga_component

            attack_parts = [(0.45, gf90 / max(base["gf90"], 0.01))]
            if sot is not None:
                attack_parts.append((0.35, sot / max(base["sot"], 0.01)))
            if big90 is not None:
                attack_parts.append((0.20, big90 / max(base["big_chances90"], 0.01)))
            total_w = sum(w for w, _ in attack_parts)
            attack_factor = sum(w * v for w, v in attack_parts) / max(total_w, 0.01)

            shot_pressure_factor = saves_pg / max(base["saves"], 0.01) if saves_pg is not None else defense_factor
            data_quality = "historical team data"
        else:
            strength = safe_float(team_strength.get(code, {}).get("strength_index"), 0.65 if row.get("relegated") else 0.50)
            defense_factor = max(0.65, min(1.35, 1.15 - 0.45 * strength))
            attack_factor = max(0.65, min(1.35, 0.80 + 0.45 * strength))
            shot_pressure_factor = defense_factor
            ga90 = target_base["ga90"] * defense_factor
            gf90 = target_base["gf90"] * attack_factor
            sot = target_base["sot"] * attack_factor
            cs_rate = None
            saves_pg = target_base["saves"] * shot_pressure_factor
            big90 = None
            data_quality = "current-roster bridge fallback"

        bridge_note = ""
        if row.get("promoted"):
            profile = PROMOTED_WSL_PROFILES.get(
                code, {"defense_bridge": 1.18, "attack_bridge": 0.86}
            )
            defense_bridge = safe_float(profile.get("defense_bridge"), 1.18)
            attack_bridge = safe_float(profile.get("attack_bridge"), 0.86)
            defense_factor *= defense_bridge
            attack_factor *= attack_bridge
            shot_pressure_factor *= 1.10
            sot = (sot if sot is not None else base["sot"] * attack_factor) * 0.92
            bridge_note = (
                f"Promoted WSL2→WSL club-specific bridge "
                f"(DEF×{defense_bridge:.2f}, ATK×{attack_bridge:.2f})"
            )
        elif row.get("relegated"):
            profile = RELEGATED_WSL2_PROFILES.get(
                code, {"defense_bridge": 0.82, "attack_bridge": 1.20}
            )
            defense_bridge = safe_float(profile.get("defense_bridge"), 0.82)
            attack_bridge = safe_float(profile.get("attack_bridge"), 1.20)
            defense_factor *= defense_bridge
            attack_factor *= attack_bridge
            shot_pressure_factor *= 0.88
            bridge_note = (
                f"Relegated WSL→WSL2 elite carryover bridge "
                f"(DEF×{defense_bridge:.2f}, ATK×{attack_bridge:.2f})"
            )

        m = manual.get(code) or {}
        defense_factor *= max(0.60, 1.0 - safe_float(m.get("defense_adjustment"), 0) / 100.0)
        attack_factor *= max(0.60, 1.0 + safe_float(m.get("attack_adjustment"), 0) / 100.0)

        defense_factor = max(0.45, min(1.75, defense_factor))
        attack_factor = max(0.45, min(1.75, attack_factor))
        shot_pressure_factor = max(0.55, min(1.65, shot_pressure_factor))

        defense_prior = max(5.0, min(95.0, 50.0 + (1.0 - defense_factor) * 45.0))
        attack_prior = max(5.0, min(95.0, 50.0 + (attack_factor - 1.0) * 45.0))

        out[code] = {
            "cs_defense_prior": round(defense_prior, 1),
            "attack_threat_prior": round(attack_prior, 1),
            "defense_factor": round(defense_factor, 4),
            "attack_factor": round(attack_factor, 4),
            "shot_pressure_factor": round(shot_pressure_factor, 4),
            "ga_per_game": round(ga90, 3),
            "gf_per_game": round(gf90, 3),
            "sot_for_per90": round(safe_float(sot), 3) if sot is not None else None,
            "clean_sheet_rate": round(cs_rate, 4) if cs_rate is not None else None,
            "saves_per_game": round(safe_float(saves_pg), 3) if saves_pg is not None else None,
            "big_chances_per90": round(big90, 3) if big90 is not None else None,
            "prior_league": prior_league,
            "target_league": target_league,
            "league_baseline_ga": round(target_base["ga90"], 3),
            "league_baseline_sot": round(target_base["sot"], 3),
            "league_baseline_saves": round(target_base["saves"], 3),
            "promoted": bool(row.get("promoted")),
            "relegated": bool(row.get("relegated")),
            "bridge_note": bridge_note,
            "data_quality": data_quality,
        }

    return out


def keeper_quality_score(player_name: str | None, model_inputs: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    name = str(player_name or "")
    row = (model_inputs.get("keepers", {}) or {}).get(name, {}) or {}
    manual = (model_inputs.get("manual_keeper_adjustments", {}) or {}).get(name, {}) or {}

    sp = row.get("keeper_save_pct")
    gp = row.get("goals_prevented")
    saves90 = row.get("saves_per90")
    ga90 = row.get("goals_conceded_per90")
    adjustment = safe_float(manual.get("quality_adjustment"), 0)

    q = 50.0 + adjustment
    evidence = []
    if _valid_number(sp):
        q += (safe_float(sp) - 70.0) * 1.7
        evidence.append(f"save% {safe_float(sp):.1f}")
    if _valid_number(gp):
        q += safe_float(gp)
        evidence.append(f"goals prevented {safe_float(gp):+.1f}")

    q = round(max(30.0, min(70.0, q)), 1)
    if not evidence:
        evidence = ["neutral (no verified individual GK prior)"]

    return q, {
        "save_pct": safe_float(sp) if _valid_number(sp) else None,
        "saves_per90": safe_float(saves90) if _valid_number(saves90) else None,
        "goals_prevented": safe_float(gp) if _valid_number(gp) else None,
        "goals_conceded_per90": safe_float(ga90) if _valid_number(ga90) else None,
        "evidence": " + ".join(evidence),
    }


def gk_fixture_scores(
    own_team: str | None,
    opponent: str | None,
    location: str | None,
    priors: dict[str, dict[str, Any]],
    keeper_name: str | None,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    own = priors.get(canonical_gk_team_code(own_team), {}) or {}
    opp = priors.get(canonical_gk_team_code(opponent), {}) or {}

    own_def_factor = safe_float(own.get("defense_factor"), 1.0)
    opp_attack_factor = safe_float(opp.get("attack_factor"), 1.0)
    baseline_ga = safe_float(own.get("league_baseline_ga"), 1.45)

    venue_goal_factor = 0.92 if location == "H" else 1.08 if location == "A" else 1.0
    # v3 calibration: opponent attack drives 65% of matchup variation; own
    # defensive prior is shrunk to 35% so elite historical defenses cannot
    # overwhelm a brutal opponent run.
    expected_ga = baseline_ga * (max(0.20, own_def_factor) ** 0.35) * (max(0.20, opp_attack_factor) ** 0.65) * venue_goal_factor
    expected_ga = max(0.25, min(3.25, expected_ga))

    cs_probability = math.exp(-expected_ga)
    three_plus_risk = poisson_prob_at_least(expected_ga, 3)
    cs_fix = 100.0 / (1.0 + math.exp(3.0 * (expected_ga - 1.35)))
    cs_fix = round(max(5.0, min(95.0, cs_fix)), 1)
    concession_safety = round(max(5.0, min(100.0, (1.0 - three_plus_risk) * 100.0)), 1)

    opp_sot = opp.get("sot_for_per90")
    if not _valid_number(opp_sot):
        opp_sot = safe_float(opp.get("league_baseline_sot"), 4.5) * opp_attack_factor

    own_pressure = safe_float(own.get("shot_pressure_factor"), own_def_factor)
    venue_shot_factor = 0.97 if location == "H" else 1.03 if location == "A" else 1.0
    estimated_sot_faced = safe_float(opp_sot) * (0.85 + 0.15 * own_pressure) * venue_shot_factor
    estimated_sot_faced = max(1.0, min(8.5, estimated_sot_faced))

    quality, qd = keeper_quality_score(keeper_name, inputs)
    save_pct = safe_float(qd.get("save_pct"), 70.0) if qd.get("save_pct") is not None else 70.0
    fixture_expected_saves = estimated_sot_faced * (save_pct / 100.0)

    if qd.get("saves_per90") is not None:
        expected_saves = 0.80 * fixture_expected_saves + 0.20 * safe_float(qd.get("saves_per90"))
    else:
        expected_saves = fixture_expected_saves

    expected_saves = max(0.5, min(6.5, expected_saves))
    save_point_probability = poisson_prob_at_least(expected_saves, 3)
    save_opportunity = round(max(5.0, min(95.0, save_point_probability * 100.0)), 1)

    gk_fix = round(max(5.0, min(
        95.0,
        0.55 * cs_fix
        + 0.25 * save_opportunity
        + 0.10 * quality
        + 0.10 * concession_safety
    )), 1)

    return {
        "cs_fix": cs_fix,
        "cs_probability": round(cs_probability, 4),
        "expected_goals_against": round(expected_ga, 3),
        "three_plus_conceded_risk": round(three_plus_risk, 4),
        "concession_safety": concession_safety,
        "estimated_sot_faced": round(estimated_sot_faced, 3),
        "expected_saves": round(expected_saves, 3),
        "save_point_probability": round(save_point_probability, 4),
        "save_opportunity": save_opportunity,
        "keeper_quality": quality,
        "gk_fix": gk_fix,
        "own_cs_prior": safe_float(own.get("cs_defense_prior"), 50.0),
        "opponent_attack_prior": safe_float(opp.get("attack_threat_prior"), 50.0),
        "opponent_sot_per90": round(safe_float(opp_sot), 3),
        "venue_adjustment": 5 if location == "H" else -5 if location == "A" else 0,
        "keeper_quality_detail": qd,
    }


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


def rank_percentile(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 0.5}
    return {
        team: idx / (len(ordered) - 1)
        for idx, (team, _) in enumerate(ordered)
    }


def build_team_strength_priors(players_raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build preseason team-strength priors from the current roster.

    Team raw score = sum of the top 11 previous-season fantasy point totals on
    the current roster. Ranking happens within the current competition first,
    so WSL2 totals are never treated as directly equal to WSL totals.

    Promoted/relegated clubs are then mapped onto a sensible destination-league
    preseason band instead of receiving a flat fixture-rating adjustment.
    """
    team_info: dict[str, dict[str, Any]] = {}

    for raw in players_raw:
        team = str(raw.get("teamAcronymName") or raw.get("teamShortName") or "").upper()
        comp = raw.get("competitionId")
        if not team or not comp:
            continue
        bucket = team_info.setdefault(team, {
            "competition_id": comp,
            "prior_points": [],
            "player_count": 0,
        })
        bucket["player_count"] += 1
        pts = safe_float(raw.get("pointsLastSeason"), 0.0)
        if pts > 0:
            bucket["prior_points"].append(pts)

    scores_by_comp: dict[str, dict[str, float]] = {}
    raw_scores: dict[str, float] = {}

    for team, info in team_info.items():
        score = sum(sorted(info["prior_points"], reverse=True)[:11])
        raw_scores[team] = score
        scores_by_comp.setdefault(info["competition_id"], {})[team] = score

    ranks_by_comp = {
        comp: rank_percentile(scores)
        for comp, scores in scores_by_comp.items()
    }

    priors: dict[str, dict[str, Any]] = {}
    for team, info in team_info.items():
        comp = info["competition_id"]
        rank = ranks_by_comp.get(comp, {}).get(team, 0.5)
        note = ""

        if comp == WSL_COMPETITION_ID:
            if team in PROMOTED_TO_WSL:
                profile = PROMOTED_WSL_PROFILES.get(team, {"strength_band": (0.20, 0.38)})
                low, high = profile["strength_band"]
                strength = low + ((high - low) * rank)
                note = f"Promoted to WSL: club-specific preseason band {low:.2f}..{high:.2f}"
            else:
                strength = 0.18 + (0.74 * rank)   # 0.18..0.92
        elif comp == WSL2_COMPETITION_ID:
            if team in RELEGATED_TO_WSL2:
                profile = RELEGATED_WSL2_PROFILES.get(team, {"strength_band": (0.82, 0.92)})
                low, high = profile["strength_band"]
                strength = low + ((high - low) * rank)
                note = f"Relegated to WSL2: elite preseason band {low:.2f}..{high:.2f}"
            else:
                strength = 0.15 + (0.70 * rank)   # 0.15..0.85
        else:
            strength = 0.25 + (0.50 * rank)

        priors[team] = {
            "competition_id": comp,
            "raw_roster_prior_points_top11": round(raw_scores.get(team, 0.0), 1),
            "within_competition_rank": round(rank, 4),
            "strength_index": round(max(0.05, min(0.95, strength)), 4),
            "player_count": info["player_count"],
            "transition_note": note,
        }

    return priors



def map_rank_to_destination_strength(
    team: str,
    competition_id: str | None,
    rank: float,
) -> tuple[float, str]:
    """Map an intra-competition rank onto a preseason destination-league scale."""
    note = ""

    if competition_id == WSL_COMPETITION_ID:
        if team in PROMOTED_TO_WSL:
            profile = PROMOTED_WSL_PROFILES.get(team, {"strength_band": (0.20, 0.38)})
            low, high = profile["strength_band"]
            strength = low + ((high - low) * rank)
            note = f"Promoted to WSL: club-specific unit band {low:.2f}..{high:.2f}"
        else:
            strength = 0.18 + (0.74 * rank)   # 0.18..0.92
    elif competition_id == WSL2_COMPETITION_ID:
        if team in RELEGATED_TO_WSL2:
            profile = RELEGATED_WSL2_PROFILES.get(team, {"strength_band": (0.82, 0.92)})
            low, high = profile["strength_band"]
            strength = low + ((high - low) * rank)
            note = f"Relegated to WSL2: elite unit band {low:.2f}..{high:.2f}"
        else:
            strength = 0.15 + (0.70 * rank)   # 0.15..0.85
    else:
        strength = 0.25 + (0.50 * rank)

    return max(0.05, min(0.95, strength)), note


def build_team_unit_priors(
    players_raw: list[dict[str, Any]],
    team_strength: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build separate preseason attacking and defensive team-strength priors.

    Attack prior: top six prior-season fantasy point totals among MID/FOR.
    Defense prior: top five prior-season fantasy point totals among GK/DEF.

    Each unit is ranked within its current competition first, then mapped onto
    the destination-league scale. We blend 70% unit-specific evidence with
    30% whole-team strength to reduce noise when a club has many new signings
    or sparse prior-season data.
    """
    units: dict[str, dict[str, Any]] = {}

    for raw in players_raw:
        team = str(raw.get("teamAcronymName") or raw.get("teamShortName") or "").upper()
        comp = raw.get("competitionId")
        if not team or not comp:
            continue

        position = POSITION_MAP.get(
            str(raw.get("skillName") or "").lower(),
            str(raw.get("skillName") or "").upper()
        )
        pts = safe_float(raw.get("pointsLastSeason"), 0.0)

        bucket = units.setdefault(team, {
            "competition_id": comp,
            "attack_points": [],
            "defense_points": [],
        })

        if pts > 0:
            if position in {"MID", "FOR"}:
                bucket["attack_points"].append(pts)
            elif position in {"GK", "DEF"}:
                bucket["defense_points"].append(pts)

    attack_scores_by_comp: dict[str, dict[str, float]] = {}
    defense_scores_by_comp: dict[str, dict[str, float]] = {}

    for team, info in units.items():
        attack_score = sum(sorted(info["attack_points"], reverse=True)[:6])
        defense_score = sum(sorted(info["defense_points"], reverse=True)[:5])
        comp = info["competition_id"]
        attack_scores_by_comp.setdefault(comp, {})[team] = attack_score
        defense_scores_by_comp.setdefault(comp, {})[team] = defense_score

    attack_ranks = {
        comp: rank_percentile(scores)
        for comp, scores in attack_scores_by_comp.items()
    }
    defense_ranks = {
        comp: rank_percentile(scores)
        for comp, scores in defense_scores_by_comp.items()
    }

    priors: dict[str, dict[str, Any]] = {}

    for team, info in units.items():
        comp = info["competition_id"]
        attack_rank = attack_ranks.get(comp, {}).get(team, 0.5)
        defense_rank = defense_ranks.get(comp, {}).get(team, 0.5)

        attack_mapped, attack_note = map_rank_to_destination_strength(team, comp, attack_rank)
        defense_mapped, defense_note = map_rank_to_destination_strength(team, comp, defense_rank)

        generic_strength = safe_float(team_strength.get(team, {}).get("strength_index"), 0.50)

        attack_strength = (0.70 * attack_mapped) + (0.30 * generic_strength)
        defense_strength = (0.70 * defense_mapped) + (0.30 * generic_strength)

        priors[team] = {
            "competition_id": comp,
            "attack_raw_points_top6": round(sum(sorted(info["attack_points"], reverse=True)[:6]), 1),
            "defense_raw_points_top5": round(sum(sorted(info["defense_points"], reverse=True)[:5]), 1),
            "attack_rank": round(attack_rank, 4),
            "defense_rank": round(defense_rank, 4),
            "attack_strength_index": round(max(0.05, min(0.95, attack_strength)), 4),
            "defense_strength_index": round(max(0.05, min(0.95, defense_strength)), 4),
            "attack_transition_note": attack_note,
            "defense_transition_note": defense_note,
        }

    return priors


def defensive_fixture_opportunity(
    own_team_code: str | None,
    opponent_code: str | None,
    location: str | None,
    unit_strength: dict[str, dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    """Schedule-only defensive fixture favorability, 0..100, higher = easier.

    This is intentionally *not* a clean-sheet projection and does not reward a
    club for being a strong defense itself. It answers only: how easy is the
    opponent/venue for a defense in this transfer leg?
    """
    opp = str(opponent_code or "").upper()
    opp_info = unit_strength.get(opp, {})
    opp_attack = safe_float(opp_info.get("attack_strength_index"), 0.50)
    venue = 5.0 if location == "H" else -5.0 if location == "A" else 0.0

    score = 50.0 + ((0.50 - opp_attack) * 70.0) + venue
    score = round(max(8.0, min(92.0, score)), 1)
    return score, {
        "opponent_attack_strength_index": round(opp_attack, 4),
        "venue_adjustment": venue,
        "opponent_attack_transition_note": opp_info.get("attack_transition_note", ""),
        "schedule_only": True,
    }


def attacking_fixture_opportunity(
    own_team_code: str | None,
    opponent_code: str | None,
    location: str | None,
    unit_strength: dict[str, dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    """Schedule-only attacking fixture favorability, 0..100, higher = easier.

    The score depends on opponent defensive strength plus venue, not the
    attacking strength of the club being ranked.
    """
    opp = str(opponent_code or "").upper()
    opp_info = unit_strength.get(opp, {})
    opp_defense = safe_float(opp_info.get("defense_strength_index"), 0.50)
    venue = 5.0 if location == "H" else -5.0 if location == "A" else 0.0

    score = 50.0 + ((0.50 - opp_defense) * 70.0) + venue
    score = round(max(8.0, min(92.0, score)), 1)
    return score, {
        "opponent_defense_strength_index": round(opp_defense, 4),
        "venue_adjustment": venue,
        "opponent_defense_transition_note": opp_info.get("defense_transition_note", ""),
        "schedule_only": True,
    }



def source_opportunity_softened(
    difficulty: float | None,
    competition_id: str | None,
    calibration: dict[str, dict[str, float]] | None,
) -> float | None:
    raw = wsl_difficulty_to_opportunity(difficulty, competition_id, calibration)
    if raw is None:
        return None
    # Convert the old 0..100 source component to 10..90 before blending.
    return round(10.0 + (0.80 * raw), 1)


def team_matchup_opportunity(
    own_team_code: str | None,
    opponent_code: str | None,
    location: str | None,
    team_strength: dict[str, dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    own = str(own_team_code or "").upper()
    opp = str(opponent_code or "").upper()

    own_info = team_strength.get(own, {})
    opp_info = team_strength.get(opp, {})
    own_strength = safe_float(own_info.get("strength_index"), 0.50)
    opp_strength = safe_float(opp_info.get("strength_index"), 0.50)

    venue = (
        HOME_ADVANTAGE_POINTS if location == "H"
        else AWAY_DISADVANTAGE_POINTS if location == "A"
        else 0.0
    )

    score = 50.0 + ((own_strength - opp_strength) * 60.0) + venue
    score = round(max(8.0, min(92.0, score)), 1)

    return score, {
        "own_strength_index": round(own_strength, 4),
        "opponent_strength_index": round(opp_strength, 4),
        "venue_adjustment": venue,
        "own_transition_note": own_info.get("transition_note", ""),
        "opponent_transition_note": opp_info.get("transition_note", ""),
    }


def blended_fixture_opportunity(
    difficulty: float | None,
    own_team_code: str | None,
    opponent_code: str | None,
    location: str | None,
    competition_id: str | None,
    calibration: dict[str, dict[str, float]] | None,
    team_strength: dict[str, dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    source_score = source_opportunity_softened(difficulty, competition_id, calibration)
    matchup_score, detail = team_matchup_opportunity(
        own_team_code, opponent_code, location, team_strength
    )

    if source_score is None:
        final = matchup_score
        source_weight = 0.0
        matchup_weight = 1.0
    else:
        source_weight = SOURCE_RATING_WEIGHT
        matchup_weight = TEAM_MATCHUP_WEIGHT
        final = (
            source_score * source_weight
            + matchup_score * matchup_weight
        )

    final = round(max(5.0, min(95.0, final)), 1)

    return final, {
        "source_opportunity_softened": source_score,
        "source_weight": source_weight,
        "team_matchup_opportunity": matchup_score,
        "team_matchup_weight": matchup_weight,
        **detail,
    }



def fixture_rating_to_score(rating: float | None) -> int | str:
    """Convert 0-100 opportunity to 1-5: 5 = easiest/best, 1 = hardest."""
    if rating is None:
        return "-"
    r = safe_float(rating, 50.0)
    if r >= 80:
        return 5
    if r >= 65:
        return 4
    if r >= 45:
        return 3
    if r >= 25:
        return 2
    return 1


def build_upcoming_fixture(
    raw: dict[str, Any],
    own_team_id: str | None = None,
    own_team_name: str | None = None,
    own_team_short_name: str | None = None,
    competition_id: str | None = None,
    fixture_calibration: dict[str, dict[str, float]] | None = None,
    team_strength: dict[str, dict[str, Any]] | None = None,
    position: str | None = None,
    player_name: str | None = None,
    gk_team_priors: dict[str, dict[str, Any]] | None = None,
    gk_model_inputs: dict[str, Any] | None = None,
    market_lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    difficulty = raw.get("currentRating")
    location = raw.get("location")
    opponent_id = raw.get("vsTeamAcronymName") or compact_id(raw.get("vsTeamId"))
    opponent_name = raw.get("vsTeamName")
    opponent_short = raw.get("vsTeamShortName")

    source_base = wsl_difficulty_to_opportunity(
        difficulty, competition_id, fixture_calibration
    )
    opportunity, model_detail = blended_fixture_opportunity(
        difficulty,
        own_team_id,
        opponent_id,
        location,
        competition_id,
        fixture_calibration,
        team_strength or {},
    )

    gk_detail: dict[str, Any] | None = None
    if position == "GK":
        gk_detail = gk_fixture_scores(
            own_team_id, opponent_id, location,
            gk_team_priors or {}, player_name, gk_model_inputs or {},
        )
        # For goalkeeper assets, Fixture Rating is the player-specific GK model.
        opportunity = gk_detail["gk_fix"]

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
    market_detail = market_fields_for_fixture(raw.get("matchId"), location, market_lookup)

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

        # Raw/source values retained for auditability.
        "current_rating": difficulty,
        "fixture_difficulty": difficulty,
        "competition_id": competition_id,
        "league": competition_label(competition_id),
        "source_base_opportunity_rating": source_base,

        # v5 model components.
        "source_opportunity_softened": model_detail.get("source_opportunity_softened"),
        "source_weight": model_detail.get("source_weight"),
        "team_matchup_opportunity": model_detail.get("team_matchup_opportunity"),
        "team_matchup_weight": model_detail.get("team_matchup_weight"),
        "own_strength_index": model_detail.get("own_strength_index"),
        "opponent_strength_index": model_detail.get("opponent_strength_index"),
        "venue_adjustment": model_detail.get("venue_adjustment"),
        "own_transition_note": model_detail.get("own_transition_note"),
        "opponent_transition_note": model_detail.get("opponent_transition_note"),

        # Goalkeeper-specific audit fields (null for non-GKs).
        "gk_model": "team-cs-save-v2" if gk_detail else None,
        "gk_cs_fix": gk_detail.get("cs_fix") if gk_detail else None,
        "gk_cs_probability": gk_detail.get("cs_probability") if gk_detail else None,
        "gk_expected_goals_against": gk_detail.get("expected_goals_against") if gk_detail else None,
        "gk_three_plus_conceded_risk": gk_detail.get("three_plus_conceded_risk") if gk_detail else None,
        "gk_concession_safety": gk_detail.get("concession_safety") if gk_detail else None,
        "gk_estimated_sot_faced": gk_detail.get("estimated_sot_faced") if gk_detail else None,
        "gk_expected_saves": gk_detail.get("expected_saves") if gk_detail else None,
        "gk_save_point_probability": gk_detail.get("save_point_probability") if gk_detail else None,
        "gk_save_opportunity": gk_detail.get("save_opportunity") if gk_detail else None,
        "gk_keeper_quality": gk_detail.get("keeper_quality") if gk_detail else None,
        "gk_keeper_save_pct": (gk_detail.get("keeper_quality_detail") or {}).get("save_pct") if gk_detail else None,
        "gk_keeper_saves_per90": (gk_detail.get("keeper_quality_detail") or {}).get("saves_per90") if gk_detail else None,
        "gk_keeper_goals_prevented": (gk_detail.get("keeper_quality_detail") or {}).get("goals_prevented") if gk_detail else None,
        "gk_keeper_quality_evidence": (gk_detail.get("keeper_quality_detail") or {}).get("evidence") if gk_detail else None,
        "gk_own_cs_prior": gk_detail.get("own_cs_prior") if gk_detail else None,
        "gk_opponent_attack_prior": gk_detail.get("opponent_attack_prior") if gk_detail else None,
        "gk_opponent_sot_per90": gk_detail.get("opponent_sot_per90") if gk_detail else None,
        "gk_venue_adjustment": gk_detail.get("venue_adjustment") if gk_detail else None,

        # Market clean-sheet probability from Pinnacle team-total U0.5, if available.
        **market_detail,

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

    lines = [
        "Upcoming fixtures — GK Model v2:" if position == "GK"
        else "Upcoming fixtures — Fixture Model v5:"
    ]
    opportunities: list[float] = []

    for f in fixtures:
        opponent = f.get("opponent_id") or f.get("opponent_short_name") or f.get("opponent_name") or "TBD"
        loc = f.get("location") or ""
        opportunity = f.get("opportunity_rating")
        score = fixture_rating_to_score(opportunity)

        lines.append(
            f"GW{f.get('game_week')}: vs {opponent} ({loc}) on "
            f"{f.get('game_date')} {f.get('kick_off_time')}"
        )

        difficulty = f.get("fixture_difficulty", f.get("current_rating"))
        if difficulty is not None:
            lines.append(f"  WSL source difficulty: {difficulty}/100 (higher = harder)")

        if f.get("source_base_opportunity_rating") is not None:
            lines.append(
                f"  Source-derived opportunity: {f.get('source_base_opportunity_rating')}/100; "
                f"softened component: {f.get('source_opportunity_softened')}/100"
            )

        lines.append(
            f"  Team matchup: {f.get('team_matchup_opportunity')}/100 "
            f"(own strength {f.get('own_strength_index')}, "
            f"opponent {f.get('opponent_strength_index')}, "
            f"venue {safe_float(f.get('venue_adjustment')):+.1f})"
        )

        notes = [
            x for x in (f.get("own_transition_note"), f.get("opponent_transition_note"))
            if x
        ]
        if notes:
            lines.append("  Division bridge: " + " | ".join(notes))

        if f.get("market_cs_probability") is not None:
            lines.append(
                f"  Pinnacle market CS: {safe_float(f.get('market_cs_probability'))*100:.1f}% "
                f"(de-vigged opponent team total U0.5)"
            )

        if opportunity is not None:
            opportunities.append(float(opportunity))
            if position == "GK" and f.get("gk_model"):
                lines.append(
                    f"  Final GK Fix: {opportunity}/100 "
                    f"(55% CS + 25% 3-save chance + 10% keeper quality + 10% 3+ concession safety); "
                    f"opportunity bucket: {score}/5"
                )
                lines.append(
                    f"  CS Fix: {f.get('gk_cs_fix')}/100 "
                    f"(CS probability {safe_float(f.get('gk_cs_probability'))*100:.0f}%) | "
                    f"Save Opp: {f.get('gk_save_opportunity')}/100 "
                    f"(3+ save probability {safe_float(f.get('gk_save_point_probability'))*100:.0f}%)"
                )
                lines.append(
                    f"  Expected GA: {f.get('gk_expected_goals_against')} | "
                    f"3+ conceded risk: {safe_float(f.get('gk_three_plus_conceded_risk'))*100:.0f}% | "
                    f"Expected saves: {f.get('gk_expected_saves')}"
                )
                lines.append(
                    f"  Keeper Quality: {f.get('gk_keeper_quality')}/100"
                )
                if f.get("gk_keeper_save_pct") is not None:
                    lines.append(f"  Keeper prior save%: {f.get('gk_keeper_save_pct')}%")
                lines.append(
                    f"  Team CS prior: {f.get('gk_own_cs_prior')}/100 | "
                    f"Opponent attack: {f.get('gk_opponent_attack_prior')}/100 | "
                    f"Venue: {safe_float(f.get('gk_venue_adjustment')):+.0f}"
                )
            else:
                lines.append(
                    f"  Final fixture opportunity: {opportunity}/100 "
                    f"({int(round(safe_float(f.get('source_weight')) * 100))}% source + "
                    f"{int(round(safe_float(f.get('team_matchup_weight')) * 100))}% team matchup); "
                    f"opportunity bucket: {score}/5"
                )

    if opportunities:
        lines.append(
            f"Average fixture opportunity: "
            f"{round(sum(opportunities) / len(opportunities), 1)}/100"
        )

    return "\\n".join(lines)



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


def transform_player(raw: dict[str, Any], fixture_calibration: dict[str, dict[str, float]], team_strength: dict[str, dict[str, Any]], gk_team_priors: dict[str, dict[str, Any]] | None = None, gk_model_inputs: dict[str, Any] | None = None, market_lookup: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
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
            competition_id, fixture_calibration, team_strength,
            position, name, gk_team_priors or {}, gk_model_inputs or {}, market_lookup or {}
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
        "Division Transition": (
            "Promoted to WSL" if (raw.get("teamAcronymName") or raw.get("teamShortName")) in PROMOTED_TO_WSL
            else "Relegated to WSL2" if (raw.get("teamAcronymName") or raw.get("teamShortName")) in RELEGATED_TO_WSL2
            else ""
        ),
        "Team Strength Index": safe_float(team_strength.get(str(own_team_id).upper(), {}).get("strength_index"), 0.5),
        "Team Strength Rank": safe_float(team_strength.get(str(own_team_id).upper(), {}).get("within_competition_rank"), 0.5),
        "Position": position,
        "GK Quality": (keeper_quality_score(name, gk_model_inputs or {})[0] if position == "GK" else None),
        "GK Save Percentage Prior": (((gk_model_inputs or {}).get("keepers", {}).get(name, {}) or {}).get("keeper_save_pct") if position == "GK" else None),
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
        "Next Market CS Probability": next_fixture.get("market_cs_probability") if next_fixture else None,
        "Next Market CS Source": next_fixture.get("market_cs_source") if next_fixture else None,
        "Next Fixture Score": fixture_rating_to_score(next_rating) if next_fixture else "-",
        "Next Fixture Details": fixture_details_text(upcoming[:1], position),
        "Following Fixture Rating": round(following_rating, 1),
        "Following Market CS Probability": following_fixture.get("market_cs_probability") if following_fixture else None,
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
    team_strength = build_team_strength_priors(players_raw)
    gk_model_inputs = load_gk_model_inputs()
    market_metadata, market_lookup = load_market_odds()
    gk_team_priors = build_gk_team_priors(gk_model_inputs, team_strength)
    team_unit_strength = build_team_unit_priors(players_raw, team_strength)
    players = [
        transform_player(p, fixture_calibration, team_strength, gk_team_priors, gk_model_inputs, market_lookup)
        for p in players_raw
    ]
    history = update_history(players)
    last_price_change = detect_last_global_price_change_date(history)

    for player in players:
        selected = safe_float(player.get("Selected Percentage"))
        player["Selected Percentage Change 1W"] = selected_delta(history, player["Name"], selected, days_back=7)
        player["Selected Percentage Change Since Last Global Price Change"] = selected_delta_since(
            history, player["Name"], selected, last_price_change
        )

    fixtures = [normalize_fixture(f) for f in fixtures_raw]
    fantasy_gameweeks = assign_canonical_fantasy_gameweeks(fixtures)

    # Attach current market clean-sheet probabilities to the full fixture schedule.
    for fixture in fixtures:
        market_row = market_lookup.get(str(fixture.get("match_id") or ""))
        if market_row:
            fixture["home_market_cs_probability"] = market_row.get("home_market_cs_probability")
            fixture["away_market_cs_probability"] = market_row.get("away_market_cs_probability")
            fixture["market_cs_source"] = market_row.get("source")
            fixture["market_cs_method"] = market_row.get("method")
            fixture["market_cs_updated_at"] = market_row.get("market_updated_at")
            fixture["home_market_cs_over_odds"] = (market_row.get("home_opponent_team_total_0_5") or {}).get("over_odds")
            fixture["home_market_cs_under_odds"] = (market_row.get("home_opponent_team_total_0_5") or {}).get("under_odds")
            fixture["away_market_cs_over_odds"] = (market_row.get("away_opponent_team_total_0_5") or {}).get("over_odds")
            fixture["away_market_cs_under_odds"] = (market_row.get("away_opponent_team_total_0_5") or {}).get("under_odds")
        else:
            fixture["home_market_cs_probability"] = None
            fixture["away_market_cs_probability"] = None
            fixture["market_cs_source"] = None
            fixture["market_cs_method"] = None
            fixture["market_cs_updated_at"] = None

    # Full-schedule general + defensive fixture opportunities for leg planning.
    # These mirror the player-level fixture model so every published GW can be
    # compared even when the player feed only exposes the next few fixtures.
    for fixture in fixtures:
        comp = fixture.get("competition_id")
        home_general, _ = blended_fixture_opportunity(
            fixture.get("home_rating"), fixture.get("home_id"), fixture.get("away_id"),
            "H", comp, fixture_calibration, team_strength
        )
        away_general, _ = blended_fixture_opportunity(
            fixture.get("away_rating"), fixture.get("away_id"), fixture.get("home_id"),
            "A", comp, fixture_calibration, team_strength
        )
        home_def, _ = defensive_fixture_opportunity(
            fixture.get("home_id"), fixture.get("away_id"), "H", team_unit_strength
        )
        away_def, _ = defensive_fixture_opportunity(
            fixture.get("away_id"), fixture.get("home_id"), "A", team_unit_strength
        )
        home_att, _ = attacking_fixture_opportunity(
            fixture.get("home_id"), fixture.get("away_id"), "H", team_unit_strength
        )
        away_att, _ = attacking_fixture_opportunity(
            fixture.get("away_id"), fixture.get("home_id"), "A", team_unit_strength
        )
        fixture["home_fixture_opportunity"] = home_general
        fixture["away_fixture_opportunity"] = away_general
        fixture["home_fixture_score"] = fixture_rating_to_score(home_general)
        fixture["away_fixture_score"] = fixture_rating_to_score(away_general)
        fixture["home_defensive_opportunity"] = home_def
        fixture["away_defensive_opportunity"] = away_def
        fixture["home_attacking_opportunity"] = home_att
        fixture["away_attacking_opportunity"] = away_att

    # Full-schedule goalkeeper model: clean-sheet environment and save opportunity.
    for fixture in fixtures:
        hs = gk_fixture_scores(fixture.get("home_id"), fixture.get("away_id"), "H", gk_team_priors, None, gk_model_inputs)
        aws = gk_fixture_scores(fixture.get("away_id"), fixture.get("home_id"), "A", gk_team_priors, None, gk_model_inputs)
        fixture["home_cs_opportunity"] = hs["cs_fix"]
        fixture["away_cs_opportunity"] = aws["cs_fix"]
        fixture["home_cs_probability"] = hs["cs_probability"]
        fixture["away_cs_probability"] = aws["cs_probability"]
        fixture["home_expected_goals_against"] = hs["expected_goals_against"]
        fixture["away_expected_goals_against"] = aws["expected_goals_against"]
        fixture["home_three_plus_conceded_risk"] = hs["three_plus_conceded_risk"]
        fixture["away_three_plus_conceded_risk"] = aws["three_plus_conceded_risk"]
        fixture["home_concession_safety"] = hs["concession_safety"]
        fixture["away_concession_safety"] = aws["concession_safety"]
        fixture["home_estimated_sot_faced"] = hs["estimated_sot_faced"]
        fixture["away_estimated_sot_faced"] = aws["estimated_sot_faced"]
        fixture["home_expected_saves_neutral"] = hs["expected_saves"]
        fixture["away_expected_saves_neutral"] = aws["expected_saves"]
        fixture["home_save_point_probability_neutral"] = hs["save_point_probability"]
        fixture["away_save_point_probability_neutral"] = aws["save_point_probability"]
        fixture["home_save_opportunity"] = hs["save_opportunity"]
        fixture["away_save_opportunity"] = aws["save_opportunity"]
        fixture["home_gk_opportunity_neutral"] = hs["gk_fix"]
        fixture["away_gk_opportunity_neutral"] = aws["gk_fix"]
        fixture["home_cs_prior"] = hs["own_cs_prior"]
        fixture["away_cs_prior"] = aws["own_cs_prior"]
        fixture["home_opponent_attack_prior"] = hs["opponent_attack_prior"]
        fixture["away_opponent_attack_prior"] = aws["opponent_attack_prior"]
        fixture["gk_rating_model"] = "team-cs-save-v4-transition-calibrated"

    teams = [normalize_team(t) for t in teams_raw]

    metadata = {
        "source": "WSL Fantasy public JSON feeds used by create-team UI",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_clean_sheet_data": {
            "available": bool(market_lookup),
            "input_file": MARKET_ODDS_PATH.name,
            "fixture_count": len(market_lookup),
            "source": market_metadata.get("source", "Pinnacle via OddsPapi" if market_lookup else None),
            "method": market_metadata.get("method"),
            "generated_at_utc": market_metadata.get("generated_at_utc"),
            "note": "Market CS% is kept separate from the independent GK model for calibration; it is not yet blended into GK Fix.",
        },
        "matchday_id": MATCHDAY_ID,
        "tour_id": TOUR_ID,
        "player_count": len(players),
        "fixture_count": len(fixtures),
        "team_count": len(teams),
        "fantasy_gameweeks": fantasy_gameweeks,
        "fantasy_legs": [
            {"leg": 1, "start_gw": 1, "end_gw": 5},
            {"leg": 2, "start_gw": 6, "end_gw": 11},
            {"leg": 3, "start_gw": 12, "end_gw": 14},
            {"leg": 4, "start_gw": 15, "end_gw": 19},
            {"leg": 5, "start_gw": 20, "end_gw": 23},
            {"leg": 6, "start_gw": 24, "end_gw": 26},
        ],
        "gk_fixture_model": {
            "version": "team-cs-save-v2",
            "note": "GK model separates clean-sheet environment, probability of reaching the 3-save point threshold, individual shot-stopping quality, and risk of the -1 penalty for conceding 3+ goals. WSL2 team priors use 2025-26 goals, clean sheets, saves/game, shots on target and big chances, with a conservative promotion bridge.",
            "weights": {"cs_fix": 0.55, "save_opportunity": 0.25, "keeper_quality": 0.10, "concession_safety": 0.10},
            "team_priors": gk_team_priors,
            "input_file": GK_MODEL_INPUTS_PATH.name,
        },
        "fantasy_calendar_model": {
            "version": "calendar-cluster-v1",
            "note": "Canonical Fantasy GWs are shared across WSL and WSL2 and are assigned chronologically from published league fixture dates. Competition-specific matchday numbers are preserved separately as league_game_week. A one-league-only window is flagged free_hit_eligible.",
            "cluster_max_gap_days": 3,
            "statuses": {
                "NORMAL": "Club has one fixture in an active league window.",
                "DOUBLE": "Club has more than one fixture in the same Fantasy GW.",
                "LEAGUE_OFF": "Club's entire league has no league fixtures in that Fantasy GW.",
                "CLUB_BLANK": "League is active but this club has no published league fixture in the window; could reflect postponement/rescheduling or an exceptional blank.",
                "TBD": "Fixture exists but date/window is not yet published."
            }
        },
        "last_global_price_change_date": last_price_change,
        "feed_urls": {key: urljoin(BASE_URL, path) for key, path in URLS.items()},
        "fixture_model": {
            "version": "wsl-team-strength-blend-v5",
            "note": "Preseason fixture opportunity blends softened WSL source currentRating (35%) with an independent team-matchup prior (65%) built from current-roster previous-season fantasy production, destination-league promotion/relegation bridging, and explicit home/away. Higher Fixture Rating = better. Routine 0/100 saturation is intentionally avoided.",
            "weights": {
                "source_rating": SOURCE_RATING_WEIGHT,
                "team_matchup": TEAM_MATCHUP_WEIGHT,
            },
            "venue": {
                "home_adjustment": HOME_ADVANTAGE_POINTS,
                "away_adjustment": AWAY_DISADVANTAGE_POINTS,
            },
            "competition_calibration": {
                competition_label(comp): {**vals, "competition_id": comp}
                for comp, vals in fixture_calibration.items()
            },
            "team_strength_priors": team_strength,
            "defensive_fixture_model": {
                "version": "unit-strength-defense-v6",
                "note": "Schedule-only attacking and defensive fixture opportunities are distinct from projected team strength. Defensive run uses only opponent attack + venue; attacking run uses only opponent defense + venue. Own team quality is intentionally excluded so the Leg Planner ranks schedule difficulty rather than projected performance.",
                "defensive_formula": "50 + (0.50 - opponent_attack_index)*70 + venue",
                "attacking_formula": "50 + (0.50 - opponent_defense_index)*70 + venue",
                "venue": {"home": 5.0, "away": -5.0},
                "team_unit_priors": team_unit_strength,
            },
            "promotion_relegation_bridge": {
                "promoted_to_wsl": sorted(PROMOTED_TO_WSL),
                "relegated_to_wsl2": sorted(RELEGATED_TO_WSL2),
                "promoted_wsl_strength_band": [0.20, 0.38],
                "established_wsl_strength_band": [0.18, 0.92],
                "relegated_wsl2_strength_band": [0.78, 0.88],
                "established_wsl2_strength_band": [0.15, 0.85],
                "note": "Cross-division status changes the preseason team-strength prior instead of adding a flat post-normalization opportunity adjustment."
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
