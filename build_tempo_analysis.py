#!/usr/bin/env python3
"""
Build the Toronto Tempo rotation-decay analysis from real WNBA Stats API data.

Data source: sportsdataverse/wehoop-wnba-stats-raw, a public GitHub repo that
mirrors raw stats.wnba.com API payloads (scraped by their own infrastructure,
updated through the current season). We fetch individual JSON files over
HTTPS from raw.githubusercontent.com -- not a full git clone (the repo is
4+ GB) and not a live call to stats.wnba.com itself (that API blocks
requests from cloud/datacenter IPs, which is what GitHub Actions runners are).

Four things get built:
  1. Per-player net rating by stint length, bucket edges set from the real
     stint-length distribution (terciles) via the `gamerotation` endpoint
     (which records every stint's check-in/check-out/point-diff directly --
     no play-by-play reconstruction needed for this part).
  2. The same buckets split by score margin at check-in (via `playbyplayv3`,
     to separate "coach pulled them because it was already going wrong"
     situations from a cleaner read).
  3. Real 5-man lineup net ratings, reconstructed by sweeping each player's
     on/off intervals and scoring each resulting lineup-segment off the same
     play-by-play score timeline.
  4. The same short-vs-long bucket pattern for two benchmark teams (best and
     worst record in the league), to see whether Tempo's pattern is unusual
     or just a league-wide coaching/substitution artifact.

Usage:
    python3 build_tempo_analysis.py --team-id 1611661332 --season 2026 \
        --out docs/index.html
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

RAW_BASE = "https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-stats-raw/main/wnba_stats/json"

BENCHMARK_TEAMS = [
    # (team_id, label) -- filled in with record at run time
    (1611661324, "Minnesota Lynx"),
    (1611661328, "Seattle Storm"),
]


def fetch_json(path: str) -> dict | None:
    url = f"{RAW_BASE}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def load_schedule(season: str) -> dict:
    return fetch_json(f"scheduleleaguev2/{season}.json")


def team_games_and_record(schedule: dict, team_id: int) -> tuple[list[dict], int, int]:
    games = {}
    wins = losses = 0
    for day in schedule["leagueSchedule"]["gameDates"]:
        for g in day["games"]:
            if g["gameLabel"] == "Preseason" or g["gameStatusText"] != "Final":
                continue
            ht, at = g["homeTeam"], g["awayTeam"]
            if ht["teamId"] != team_id and at["teamId"] != team_id:
                continue
            home = ht["teamId"] == team_id
            opp = at if home else ht
            mine = ht if home else at
            games[g["gameId"]] = {
                "gameId": g["gameId"], "date": g["gameDateEst"][:10],
                "opponent": opp["teamTricode"], "home": home,
            }
            if mine["score"] > opp["score"]:
                wins += 1
            else:
                losses += 1
    return list(games.values()), wins, losses


def fetch_gamerotation_stints(season: str, team_id: int, games: list[dict], with_raw_times=False) -> list[dict]:
    stints = []
    for g in games:
        payload = fetch_json(f"gamerotation/{season}/{g['gameId']}.json")
        if payload is None:
            continue
        for rs in payload["resultSets"]:
            idx = {h: i for i, h in enumerate(rs["headers"])}
            for r in rs["rowSet"]:
                if r[idx["TEAM_ID"]] != team_id:
                    continue
                in_s = r[idx["IN_TIME_REAL"]] / 10.0
                out_s = r[idx["OUT_TIME_REAL"]] / 10.0
                row = {
                    "gameId": g["gameId"], "opponent": g["opponent"], "date": g["date"],
                    "home": g["home"],
                    "pId": r[idx["PERSON_ID"]],
                    "name": f"{r[idx['PLAYER_FIRST']]} {r[idx['PLAYER_LAST']]}",
                    "elapsed_s": out_s - in_s,
                    "pt_diff": r[idx["PT_DIFF"]],
                }
                if with_raw_times:
                    row["in_s"] = in_s
                    row["out_s"] = out_s
                stints.append(row)
    return stints


# ---- play-by-play / score timeline -----------------------------------------

_CLOCK_RE = re.compile(r"PT(\d+)M([\d.]+)S")


def _clock_to_seconds(clock: str) -> float:
    m = _CLOCK_RE.match(clock)
    return int(m.group(1)) * 60 + float(m.group(2))


def _period_offset(period: int) -> int:
    return (period - 1) * 600 if period <= 4 else 2400 + (period - 5) * 300


def _period_len(period: int) -> int:
    return 600 if period <= 4 else 300


def build_score_timeline(season: str, game_id: str) -> list[tuple[float, int, int]] | None:
    payload = fetch_json(f"playbyplayv3/{season}/{game_id}.json")
    if payload is None:
        return None
    timeline = []
    for a in payload["game"]["actions"]:
        if not a.get("scoreHome"):
            continue
        p = a["period"]
        remaining = _clock_to_seconds(a["clock"])
        elapsed = _period_offset(p) + (_period_len(p) - remaining)
        timeline.append((elapsed, int(a["scoreHome"]), int(a["scoreAway"])))
    timeline.sort()
    return timeline


def score_at(timeline: list[tuple[float, int, int]], t: float) -> tuple[int, int]:
    best = (0, 0)
    for e, h, a in timeline:
        if e <= t:
            best = (h, a)
        else:
            break
    return best


def add_margin_at_checkin(season: str, team_id: int, stints: list[dict]) -> None:
    """Mutates stints in place, adding margin_at_checkin from each game's score timeline."""
    by_game = defaultdict(list)
    for s in stints:
        by_game[s["gameId"]].append(s)
    for gid, glist in by_game.items():
        tl = build_score_timeline(season, gid)
        if tl is None:
            for s in glist:
                s["margin_at_checkin"] = None
            continue
        home = glist[0]["home"]
        for s in glist:
            h, a = score_at(tl, s["in_s"])
            tempo, opp = (h, a) if home else (a, h)
            s["margin_at_checkin"] = tempo - opp


# ---- bucketing ---------------------------------------------------------------

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


def bucket_labels(e1: float, e2: float) -> list[str]:
    return [f"0-{e1:g} min", f"{e1:g}-{e2:g} min", f"{e2:g}+ min"]


def aggregate_buckets(stints: list[dict], e1: float, e2: float) -> dict:
    labels = bucket_labels(e1, e2)
    by_bucket = defaultdict(lambda: {"minutes": 0.0, "net_pts": 0.0, "stints": 0})
    for s in stints:
        m = s["elapsed_s"] / 60.0
        b = bucket_for(m, e1, e2, labels)
        by_bucket[b]["minutes"] += m
        by_bucket[b]["net_pts"] += s["pt_diff"]
        by_bucket[b]["stints"] += 1
    out = {}
    for b in labels:
        d = by_bucket[b]
        per5 = d["net_pts"] / d["minutes"] * 5 if d["minutes"] else None
        out[b] = None if per5 is None else {"per5min": round(per5, 2), "minutes": round(d["minutes"], 1), "stints": d["stints"]}
    return out


def build_player_decay(stints: list[dict], e1: float, e2: float, min_total_minutes: float = 40.0):
    labels = bucket_labels(e1, e2)
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


def build_histogram(stints: list[dict], tail_start: int = 20):
    lens_min = [s["elapsed_s"] / 60.0 for s in stints]
    bins = Counter()
    for v in lens_min:
        b = int(v) if v < tail_start else tail_start
        bins[b] += 1
    hist = [{"bin": (f"{b}-{b+1}" if b < tail_start else f"{tail_start}+"), "count": bins.get(b, 0)}
            for b in range(0, tail_start + 1)]
    return hist, round(max(lens_min), 1)


# ---- lineups ------------------------------------------------------------------

def build_lineups(season: str, stints_with_times: list[dict], min_minutes: float = 15.0) -> list[dict]:
    by_game = defaultdict(list)
    for s in stints_with_times:
        by_game[s["gameId"]].append(s)

    names = {s["pId"]: s["name"] for s in stints_with_times}
    lineup_agg = defaultdict(lambda: {"seconds": 0.0, "net_pts": 0.0, "segments": 0})

    for gid, glist in by_game.items():
        tl = build_score_timeline(season, gid)
        if tl is None:
            continue
        home = glist[0]["home"]
        times = sorted(set([s["in_s"] for s in glist] + [s["out_s"] for s in glist]))
        for t0, t1 in zip(times, times[1:]):
            if t1 - t0 <= 0:
                continue
            on_court = [s["pId"] for s in glist if s["in_s"] <= t0 and s["out_s"] >= t1]
            if len(on_court) != 5:
                continue
            h0, a0 = score_at(tl, t0)
            h1, a1 = score_at(tl, t1)
            tempo0, opp0 = (h0, a0) if home else (a0, h0)
            tempo1, opp1 = (h1, a1) if home else (a1, h1)
            net = (tempo1 - opp1) - (tempo0 - opp0)
            key = tuple(sorted(on_court))
            lineup_agg[key]["seconds"] += (t1 - t0)
            lineup_agg[key]["net_pts"] += net
            lineup_agg[key]["segments"] += 1

    rows = []
    for key, d in lineup_agg.items():
        minutes = d["seconds"] / 60.0
        if minutes < min_minutes:
            continue
        per5 = d["net_pts"] / minutes * 5
        rows.append({
            "names": [names[p] for p in key],
            "minutes": round(minutes, 1),
            "per5min": round(per5, 2),
            "segments": d["segments"],
        })
    rows.sort(key=lambda r: -r["minutes"])
    return rows


# ---- benchmark teams ------------------------------------------------------------

def build_benchmarks(season: str, schedule: dict, e1: float, e2: float, exclude_team_id: int) -> list[dict]:
    out = []
    for team_id, label in BENCHMARK_TEAMS:
        if team_id == exclude_team_id:
            continue
        games, wins, losses = team_games_and_record(schedule, team_id)
        stints = fetch_gamerotation_stints(season, team_id, games)
        buckets = aggregate_buckets(stints, e1, e2)
        out.append({"label": label, "record": f"{wins}-{losses}", "buckets": buckets})
    return out


# ---- render ------------------------------------------------------------------

def render(template_path: Path, out_path: Path, *, data, buckets, hist, edges, n_stints, max_stint,
           tail_start, margin_split, benchmarks, lineups):
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
    tpl = tpl.replace("__MARGIN_SPLIT_JSON__", json.dumps(margin_split))
    tpl = tpl.replace("__BENCHMARKS_JSON__", json.dumps(benchmarks))
    tpl = tpl.replace("__LINEUPS_JSON__", json.dumps(lineups))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(tpl)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", type=int, default=1611661332, help="WNBA team id (default: Toronto Tempo)")
    ap.add_argument("--season", default="2026")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--close-margin", type=float, default=10.0, help="abs score margin at check-in considered 'close'")
    args = ap.parse_args()

    print(f"Fetching schedule for season {args.season}...")
    schedule = load_schedule(args.season)
    games, wins, losses = team_games_and_record(schedule, args.team_id)
    print(f"{len(games)} non-preseason final games ({wins}-{losses})")

    print("Fetching gamerotation data (stint boundaries)...")
    stints = fetch_gamerotation_stints(args.season, args.team_id, games, with_raw_times=True)
    print(f"{len(stints)} stints extracted")

    e1, e2 = tercile_edges(stints)
    print(f"Bucket edges (terciles): {e1} min, {e2} min")

    buckets, players = build_player_decay(stints, e1, e2)
    hist, max_stint = build_histogram(stints)

    print("Fetching play-by-play for score-margin-at-checkin + lineups...")
    add_margin_at_checkin(args.season, args.team_id, stints)
    close = [s for s in stints if s["margin_at_checkin"] is not None and abs(s["margin_at_checkin"]) <= args.close_margin]
    blowout = [s for s in stints if s["margin_at_checkin"] is not None and abs(s["margin_at_checkin"]) > args.close_margin]
    margin_split = {
        "all": aggregate_buckets(stints, e1, e2),
        "close": aggregate_buckets(close, e1, e2),
        "blowout": aggregate_buckets(blowout, e1, e2),
    }
    print(f"  close: {len(close)} stints, blowout: {len(blowout)} stints")

    print("Reconstructing 5-man lineups...")
    lineups = build_lineups(args.season, stints)
    print(f"  {len(lineups)} lineups with >=15 min")

    print("Fetching benchmark teams...")
    benchmarks = build_benchmarks(args.season, schedule, e1, e2, exclude_team_id=args.team_id)
    for b in benchmarks:
        print(f"  {b['label']} ({b['record']})")

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stints.json").write_text(json.dumps(stints))
    (data_dir / "player_decay.json").write_text(json.dumps({"bucket_order": buckets, "edges": [e1, e2], "players": players}, indent=2))
    (data_dir / "margin_split.json").write_text(json.dumps(margin_split, indent=2))
    (data_dir / "lineups.json").write_text(json.dumps(lineups, indent=2))
    (data_dir / "benchmarks.json").write_text(json.dumps(benchmarks, indent=2))
    print(f"Wrote data files to {data_dir}/")

    render(
        Path(args.template), Path(args.out),
        data=players, buckets=buckets, hist=hist, edges=(e1, e2),
        n_stints=len(stints), max_stint=max_stint, tail_start=20,
        margin_split=margin_split, benchmarks=benchmarks, lineups=lineups,
    )
    print(f"Rendered {args.out}")


if __name__ == "__main__":
    main()
