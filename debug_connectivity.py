#!/usr/bin/env python3
"""One-off connectivity probe -- which WNBA/NBA-family API hosts are
reachable from this runner, and with what headers. Not part of the
pipeline; delete once the real endpoint is confirmed.
"""
import time
import requests

HEADERS_STATS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.wnba.com/",
    "Origin": "https://www.wnba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}
HEADERS_PLAIN = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

PROBES = [
    ("stats.wnba.com teamgamelog", HEADERS_STATS,
     "https://stats.wnba.com/stats/teamgamelog",
     {"TeamID": "1611661332", "Season": "2026", "SeasonType": "Regular Season", "LeagueID": "10"}),
    ("stats.nba.com teamgamelog (LeagueID 10)", HEADERS_STATS,
     "https://stats.nba.com/stats/teamgamelog",
     {"TeamID": "1611661332", "Season": "2026", "SeasonType": "Regular Season", "LeagueID": "10"}),
    ("cdn.wnba.com schedule", HEADERS_PLAIN,
     "https://cdn.wnba.com/static/json/staticData/scheduleLeagueV2_1.json", {}),
    ("core-api.wnba.com", HEADERS_PLAIN,
     "https://core-api.wnba.com/wnba/v1/schedule", {}),
    ("data.wnba.com", HEADERS_PLAIN,
     "https://data.wnba.com/data/10s/v2015/json/mobile_teams/wnba/2026/teams/tempo_schedule.json", {}),
]

for label, headers, url, params in PROBES:
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        dt = time.time() - t0
        print(f"[{label}] status={r.status_code} time={dt:.1f}s len={len(r.content)} ct={r.headers.get('content-type')}")
        print(f"  body[:200]={r.text[:200]!r}")
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        print(f"[{label}] FAILED after {dt:.1f}s: {type(exc).__name__}: {exc}")
    print()
