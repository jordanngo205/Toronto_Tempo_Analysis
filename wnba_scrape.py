#!/usr/bin/env python3
"""
WNBA play-by-play scraper for a single team's season, built for the
rotation/fatigue-decay analysis (same shape as fiba_scrape.py's output).

Uses the stats.wnba.com JSON API (same family as stats.nba.com). Writes:
  <outdir>/<Team> <Season> - game details.csv
  <outdir>/<Team> <Season> - pbp.csv
  <outdir>/<Team> <Season> - player box scores.csv   (Starter flag only)

Usage:
    python3 wnba_scrape.py --team-id 1611661332 --team-name "Toronto Tempo" \
                            --season 2026 --name "Toronto Tempo 2026"
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cffi_requests

BASE = "https://stats.wnba.com/stats"
HEADERS = {
    "Referer": "https://stats.wnba.com/",
    "Origin": "https://stats.wnba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}
# stats.wnba.com/stats.nba.com block on TLS fingerprint (Akamai), not just
# headers -- plain `requests` gets silently black-holed from cloud/datacenter
# IPs. curl_cffi impersonates a real Chrome TLS handshake to get through.
SESSION = cffi_requests.Session(impersonate="chrome", headers=HEADERS)

# WNBA quarters are 10 minutes (600s), OT is 5 minutes (300s) -- same as FIBA.
PERIOD_LEN = {1: 600, 2: 600, 3: 600, 4: 600}
OT_LEN = 300


def fetch_json(endpoint: str, params: dict, tries: int = 5, pause: float = 3.0) -> dict:
    url = f"{BASE}/{endpoint}"
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  retry {attempt}/{tries} on {endpoint} — {type(exc).__name__}: {exc}")
            if attempt < tries:
                time.sleep(pause * attempt)
    raise RuntimeError(f"Failed to fetch {endpoint} after {tries} attempts") from last


def result_set_df(payload: dict, name: str | None = None) -> pd.DataFrame:
    sets = payload.get("resultSets") or payload.get("resultSet")
    if isinstance(sets, dict):
        sets = [sets]
    for rs in sets:
        if name is None or rs.get("name") == name:
            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])
    raise KeyError(f"resultSet {name!r} not found; have {[s.get('name') for s in sets]}")


def team_game_ids(team_id: str, season: str) -> pd.DataFrame:
    payload = fetch_json("teamgamelog", {
        "TeamID": team_id, "Season": season, "SeasonType": "Regular Season",
        "LeagueID": "10",  # WNBA league id in the stats.nba.com-family API
    })
    df = result_set_df(payload)
    print(f"Found {len(df)} games for team {team_id}, season {season}")
    return df


def scrape_game(game_id: str) -> dict:
    pbp_payload = fetch_json("playbyplayv2", {"GameID": game_id, "StartPeriod": 0, "EndPeriod": 10})
    pbp = result_set_df(pbp_payload, "PlayByPlay")

    box_payload = fetch_json("boxscoretraditionalv2", {
        "GameID": game_id, "StartPeriod": 0, "EndPeriod": 10,
        "StartRange": 0, "EndRange": 0, "RangeType": 0,
    })
    box = result_set_df(box_payload, "PlayerStats")
    summary = result_set_df(box_payload, "TeamStats")

    return {"pbp": pbp, "box": box, "summary": summary}


def period_offset(period: int) -> int:
    if period <= 4:
        return (period - 1) * 600
    return 2400 + (period - 5) * 300


def clock_to_seconds(pctimestring: str) -> float:
    # "10:23" minutes:seconds remaining in the period.
    m, s = pctimestring.split(":")
    return int(m) * 60 + float(s)


def enrich_pbp(pbp: pd.DataFrame) -> pd.DataFrame:
    d = pbp.copy()
    d["period_i"] = pd.to_numeric(d["PERIOD"], errors="coerce")
    period_len = d["period_i"].map(lambda p: 600 if p <= 4 else 300)
    remaining = d["PCTIMESTRING"].apply(clock_to_seconds)
    d["seconds_elapsed"] = d["period_i"].map(period_offset) + (period_len - remaining)
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", required=True)
    ap.add_argument("--team-name", required=True)
    ap.add_argument("--season", required=True, help="e.g. 2026")
    ap.add_argument("--name", required=True, help="output folder / file prefix")
    args = ap.parse_args()

    outdir = Path(args.name) / "data"
    outdir.mkdir(parents=True, exist_ok=True)

    def p(kind: str) -> Path:
        return outdir / f"{args.name} - {kind}.csv"

    games = team_game_ids(args.team_id, args.season)
    if games.empty:
        raise SystemExit("No games found — check team id / season / league id.")

    all_pbp, all_box, all_details = [], [], []
    for i, row in enumerate(games.itertuples(), 1):
        gid = row.Game_ID if hasattr(row, "Game_ID") else row.GAME_ID
        print(f"[{i}/{len(games)}] game {gid} ({row.GAME_DATE} vs {row.MATCHUP})")
        try:
            got = scrape_game(gid)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {gid}: {exc}")
            continue
        pbp = enrich_pbp(got["pbp"])
        pbp["gameId"] = gid
        all_pbp.append(pbp)

        box = got["box"].copy()
        box["gameId"] = gid
        all_box.append(box)

        all_details.append({
            "gameId": gid, "date": row.GAME_DATE, "matchup": row.MATCHUP,
            "wl": row.WL, "team_id": args.team_id,
        })
        time.sleep(0.6)  # be polite to the API

    pd.concat(all_pbp, ignore_index=True).to_csv(p("pbp"), index=False)
    pd.concat(all_box, ignore_index=True).to_csv(p("player box scores"), index=False)
    pd.DataFrame(all_details).to_csv(p("game details"), index=False)
    print(f"\nWrote {len(all_details)} games to {outdir}/")


if __name__ == "__main__":
    main()
