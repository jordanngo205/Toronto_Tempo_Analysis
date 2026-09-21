#!/usr/bin/env python3
"""
Build the Toronto Tempo rotation-decay analysis from real WNBA Stats API data.

Data source: sportsdataverse/wehoop-wnba-stats-raw, a public GitHub repo that
mirrors raw stats.wnba.com API payloads (scraped by their own infrastructure,
updated through the current season). We fetch individual JSON files over
HTTPS from raw.githubusercontent.com -- not a full git clone (the repo is
4+ GB) and not a live call to stats.wnba.com itself (that API blocks
requests from cloud/datacenter IPs, which is what GitHub Actions runners are).

Uses the `gamerotation` endpoint, which records every continuous on-court
stint directly (check-in time, check-out time, point differential across
that window) -- no play-by-play reconstruction needed.

Bucket edges are derived from the real stint-length distribution (terciles),
not guessed.

Usage:
    python3 build_tempo_analysis.py --team-id 1611661332 --season 2026 \
        --out docs/index.html
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

RAW_BASE = "https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-stats-raw/main/wnba_stats/json"


def fetch_json(path: str) -> dict:
    url = f"{RAW_BASE}/{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def find_team_games(season: str, team_id: int) -> list[dict]:
    d = fetch_json(f"scheduleleaguev2/{season}.json")
    games = {}
    for day in d["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            if g["gameLabel"] == "Preseason" or g["gameStatusText"] != "Final":
                continue
            ht, at = g["homeTeam"], g["awayTeam"]
            if ht["teamId"] != team_id and at["teamId"] != team_id:
                continue
            opp = at if ht["teamId"] == team_id else ht
            games[g["gameId"]] = {
                "gameId": g["gameId"], "date": g["gameDateEst"][:10],
                "opponent": opp["teamTricode"],
            }
    return list(games.values())


def fetch_stints(season: str, team_id: int, games: list[dict]) -> list[dict]:
    stints = []
    for g in games:
        payload = fetch_json(f"gamerotation/{season}/{g['gameId']}.json")
        for rs in payload["resultSets"]:
            idx = {h: i for i, h in enumerate(rs["headers"])}
            for r in rs["rowSet"]:
                if r[idx["TEAM_ID"]] != team_id:
                    continue
                in_s = r[idx["IN_TIME_REAL"]] / 10.0
                out_s = r[idx["OUT_TIME_REAL"]] / 10.0
                stints.append({
                    "gameId": g["gameId"], "opponent": g["opponent"], "date": g["date"],
                    "pId": r[idx["PERSON_ID"]],
                    "name": f"{r[idx['PLAYER_FIRST']]} {r[idx['PLAYER_LAST']]}",
                    "elapsed_s": out_s - in_s,
                    "pt_diff": r[idx["PT_DIFF"]],
                })
    return stints


def tercile_edges(stints: list[dict]) -> tuple[float, float]:
    lens = sorted(s["elapsed_s"] / 60.0 for s in stints)
    n = len(lens)
    e1 = lens[int(33 / 100 * (n - 1))]
    e2 = lens[int(66 / 100 * (n - 1))]
    return round(e1, 1), round(e2, 1)


def bucket_for(elapsed_min: float, e1: float, e2: float, labels: list[str]) -> str:
    if elapsed_min < e1:
        return labels[0]
    if elapsed_min < e2:
        return labels[1]
    return labels[2]


def build_player_decay(stints: list[dict], e1: float, e2: float, min_total_minutes: float = 40.0):
    labels = [f"0-{e1:g} min", f"{e1:g}-{e2:g} min", f"{e2:g}+ min"]
    by_pb = defaultdict(lambda: {"minutes": 0.0, "net_pts": 0.0, "stints": 0})
    names, stint_counts, total_minutes = {}, defaultdict(int), defaultdict(float)

    for s in stints:
        m = s["elapsed_s"] / 60.0
        b = bucket_for(m, e1, e2, labels)
        key = (s["pId"], b)
        by_pb[key]["minutes"] += m
        by_pb[key]["net_pts"] += s["pt_diff"]
        by_pb[key]["stints"] += 1
        names[s["pId"]] = s["name"]
        stint_counts[s["pId"]] += 1
        total_minutes[s["pId"]] += m

    qualified = [pid for pid, m in total_minutes.items() if m >= min_total_minutes]
    result = []
    for pid in qualified:
        row = {"pId": pid, "name": names[pid], "stints": stint_counts[pid],
               "total_minutes": round(total_minutes[pid], 1), "buckets": {}}
        for b in labels:
            d = by_pb.get((pid, b))
            if d and d["minutes"] >= 5:
                per5 = d["net_pts"] / d["minutes"] * 5
                row["buckets"][b] = {"per5min": round(per5, 2), "minutes": round(d["minutes"], 1), "stints": d["stints"]}
            else:
                row["buckets"][b] = None
        result.append(row)
    result.sort(key=lambda r: -r["stints"])
    return labels, result


def build_histogram(stints: list[dict], e1: float, e2: float, tail_start: int = 20):
    lens_min = [s["elapsed_s"] / 60.0 for s in stints]
    bins = Counter()
    for v in lens_min:
        b = int(v) if v < tail_start else tail_start
        bins[b] += 1
    hist = [{"bin": (f"{b}-{b+1}" if b < tail_start else f"{tail_start}+"), "count": bins.get(b, 0)}
            for b in range(0, tail_start + 1)]
    return hist, round(max(lens_min), 1)


def render(template_path: Path, out_path: Path, *, data, buckets, hist, edges, n_stints, max_stint, tail_start):
    tpl = template_path.read_text()
    tpl = tpl.replace("__DATA_JSON__", json.dumps(data))
    tpl = tpl.replace("__BUCKETS_JSON__", json.dumps(buckets))
    tpl = tpl.replace("__HIST_JSON__", json.dumps(hist))
    tpl = tpl.replace("__EDGES_JSON__", json.dumps(list(edges)))
    tpl = tpl.replace("__B0__", buckets[0]).replace("__B1__", buckets[1]).replace("__B2__", buckets[2])
    tpl = tpl.replace("__N_STINTS__", str(n_stints))
    tpl = tpl.replace("__MAX_STINT__", str(max_stint))
    tpl = tpl.replace("__TAIL_LABEL__", f"{tail_start}+")
    tpl = tpl.replace("__EDGE0__", f"{edges[0]:g}").replace("__EDGE1__", f"{edges[1]:g}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(tpl)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", type=int, default=1611661332, help="WNBA team id (default: Toronto Tempo)")
    ap.add_argument("--season", default="2026")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    print(f"Fetching schedule for season {args.season}...")
    games = find_team_games(args.season, args.team_id)
    print(f"{len(games)} non-preseason final games")

    print("Fetching gamerotation data for each game...")
    stints = fetch_stints(args.season, args.team_id, games)
    print(f"{len(stints)} stints extracted")

    e1, e2 = tercile_edges(stints)
    print(f"Bucket edges (terciles): {e1} min, {e2} min")

    buckets, players = build_player_decay(stints, e1, e2)
    hist, max_stint = build_histogram(stints, e1, e2)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stints.json").write_text(json.dumps(stints))
    (data_dir / "player_decay.json").write_text(json.dumps({"bucket_order": buckets, "edges": [e1, e2], "players": players}, indent=2))
    print(f"Wrote {data_dir}/stints.json and {data_dir}/player_decay.json")

    render(
        Path(args.template), Path(args.out),
        data=players, buckets=buckets, hist=hist, edges=(e1, e2),
        n_stints=len(stints), max_stint=max_stint, tail_start=20,
    )
    print(f"Rendered {args.out}")


if __name__ == "__main__":
    main()
