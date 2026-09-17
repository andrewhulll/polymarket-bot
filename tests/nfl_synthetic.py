"""Synthetic NFL-like seasons with known score covariance (test helper, no network).

Scores are drawn from the model the estimator assumes -- ``(S_home, S_away)``
bivariate normal around the closing-line implied means with
``sigma^2(mu) = a + b*mu`` and correlation ``rho`` -- then rounded to integers.
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from combo_mm.nfl.ingest import Game

TRUE_A = 50.0
TRUE_B = 1.4
TRUE_RHO = 0.10
TEAMS = [f"T{i:02d}" for i in range(12)]


def _american(p: float) -> float:
    p = min(max(p, 0.02), 0.98)
    return -100.0 * p / (1 - p) if p >= 0.5 else 100.0 * (1 - p) / p


def make_games(seasons=range(2010, 2016), weeks: int = 12, seed: int = 7,
               a: float = TRUE_A, b: float = TRUE_B, rho: float = TRUE_RHO,
               unplayed_from: Optional[tuple] = None, integer_scores: bool = True,
               spread_sd: float = 5.5, total_sd: float = 4.0) -> List[Game]:
    """One game per team per week (random pairings).

    ``unplayed_from=(season, week)`` leaves that week and later without scores.
    """
    rng = np.random.default_rng(seed)
    games: List[Game] = []
    for season in seasons:
        for week in range(1, weeks + 1):
            order = rng.permutation(len(TEAMS))
            for k in range(0, len(order), 2):
                home, away = TEAMS[order[k]], TEAMS[order[k + 1]]
                spread = float(np.round(rng.normal(2.0, spread_sd) * 2) / 2)
                if spread == 0:
                    spread = 0.5
                total = float(np.round(rng.normal(45.0, total_sd) * 2) / 2)
                mu_h, mu_a = (total + spread) / 2, (total - spread) / 2
                sh, sa = math.sqrt(a + b * mu_h), math.sqrt(a + b * mu_a)
                cov = [[sh * sh, rho * sh * sa], [rho * sh * sa, sa * sa]]
                s = rng.multivariate_normal([mu_h, mu_a], cov)
                if integer_scores:
                    hs, as_ = int(max(0, round(s[0]))), int(max(0, round(s[1])))
                else:
                    hs, as_ = s[0], s[1]
                played = unplayed_from is None or (season, week) < unplayed_from
                p_home_win = 0.5 * math.erfc(-spread / (13.5 * math.sqrt(2)))
                games.append(Game(
                    game_id=f"{season}_{week:02d}_{away}_{home}", season=season, week=week,
                    game_type="REG", gameday=f"{season}-09-{week:02d}", home=home, away=away,
                    home_score=hs if played else None, away_score=as_ if played else None,
                    overtime=False, neutral=False, spread_line=spread, total_line=total,
                    home_moneyline=_american(p_home_win), away_moneyline=_american(1 - p_home_win),
                    home_spread_odds=-110.0, away_spread_odds=-110.0,
                    over_odds=-110.0, under_odds=-110.0,
                ))
    return games


GAMES_CSV_HEADER = (
    "game_id,season,game_type,week,gameday,weekday,gametime,away_team,away_score,home_team,"
    "home_score,location,result,total,overtime,old_game_id,gsis,nfl_detail_id,pfr,pff,espn,ftn,"
    "away_rest,home_rest,away_moneyline,home_moneyline,spread_line,away_spread_odds,"
    "home_spread_odds,total_line,under_odds,over_odds,div_game,roof,surface,temp,wind,away_qb_id,"
    "home_qb_id,away_qb_name,home_qb_name,away_coach,home_coach,referee,stadium_id,stadium"
)


def csv_row(game_id, season, week, away, away_score, home, home_score, spread, total,
            game_type="REG", location="Home", ml=("", ""), spread_odds=("-110", "-110"),
            total_odds=("-110", "-110"), gameday=None, gametime="") -> str:
    """One nflverse-shaped CSV row (unused columns blank)."""
    result = "" if home_score == "" else str(int(home_score) - int(away_score))
    tot = "" if home_score == "" else str(int(home_score) + int(away_score))
    cols = {
        "game_id": game_id, "season": season, "game_type": game_type, "week": week,
        "gameday": gameday or f"{season}-09-10", "gametime": gametime,
        "away_team": away, "away_score": away_score,
        "home_team": home, "home_score": home_score, "location": location, "result": result,
        "total": tot, "overtime": "0", "away_moneyline": ml[0], "home_moneyline": ml[1],
        "spread_line": spread, "away_spread_odds": spread_odds[0], "home_spread_odds": spread_odds[1],
        "total_line": total, "under_odds": total_odds[0], "over_odds": total_odds[1], "div_game": "0",
    }
    return ",".join(str(cols.get(name, "")) for name in GAMES_CSV_HEADER.split(","))
