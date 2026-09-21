#!/usr/bin/env python3
"""
Build the Toronto Tempo rotation-decay analysis from real WNBA Stats API data.

Data source: sportsdataverse/wehoop-wnba-stats-raw, a public GitHub repo that
mirrors raw stats.wnba.com API payloads (scraped by their own infrastructure,
updated through the current season). We fetch individual JSON files over
HTTPS from raw.githubusercontent.com -- not a full git clone (the repo is
4+ GB) and not a live call to stats.wnba.com itself (that API blocks
requests from cloud/datacenter IPs, which is what GitHub Actions runners are).

Three things ship on the dashboard (net-rating-by-stint-bucket, score-margin
control, league benchmark, lineup-decay, and foul-trouble cost were all
tried and cut -- they either repeated the same confounded story or didn't
hold up; see git history if you want them back):
  1. Shot-quality decay: unlike net rating (confounded by the other 9
     players on the floor), a player's OWN shot selection -- eFG%, shot
     distance -- as a function of elapsed time in their current stint is
     an individually attributable signal. Every shot's exact location and
     result comes straight from `playbyplayv3`.
  2. Within-stint "cliff finder": every stint sliced into 1-minute windows
     (not classified by its final length), pooled across every stint that
     reached that minute, then adjusted against the team's own rate at
     that same minute (leave-one-out) -- so a player's own cliff point
     survives the adjustment instead of getting smeared into one coarse
     bucket number.
  3. Real 5-man lineup net ratings, reconstructed by sweeping each player's
     on/off intervals and scoring each resulting lineup-segment off the same
     play-by-play score timeline.

Usage:
    python3 build_tempo_analysis.py --team-id 1611661332 --season 2026 \
        --out docs/index.html
"""
from __future__ import annotations

import argparse
import functools
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


@functools.lru_cache(maxsize=None)
def fetch_playbyplay_actions(season: str, game_id: str) -> list[dict] | None:
    payload = fetch_json(f"playbyplayv3/{season}/{game_id}.json")
    if payload is None:
        return None
    return payload["game"]["actions"]


def action_elapsed_s(a: dict) -> float:
    p = a["period"]
    remaining = _clock_to_seconds(a["clock"])
    return _period_offset(p) + (_period_len(p) - remaining)


def build_score_timeline(season: str, game_id: str) -> list[tuple[float, int, int]] | None:
    actions = fetch_playbyplay_actions(season, game_id)
    if actions is None:
        return None
    timeline = []
    for a in actions:
        if not a.get("scoreHome"):
            continue
        timeline.append((action_elapsed_s(a), int(a["scoreHome"]), int(a["scoreAway"])))
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


def fetch_shots_and_fouls(season: str, team_id: int, games: list[dict]) -> tuple[list[dict], list[dict]]:
    """Every Tempo field-goal attempt (with shot quality) and personal foul,
    across all games, with the raw elapsed-game-clock time attached."""
    _FOUL_RE = re.compile(r"\(P(\d+)\.")
    shots, fouls = [], []
    for g in games:
        actions = fetch_playbyplay_actions(season, g["gameId"])
        if actions is None:
            continue
        for a in actions:
            if a.get("teamId") != team_id:
                continue
            elapsed = action_elapsed_s(a)
            if a["actionType"] in ("Made Shot", "Missed Shot"):
                shots.append({
                    "gameId": g["gameId"], "pId": a["personId"], "name": a["playerName"],
                    "elapsed_s": elapsed, "period": a["period"],
                    "made": a["actionType"] == "Made Shot",
                    "shotValue": a["shotValue"], "shotDistance": a["shotDistance"],
                })
            elif a["actionType"] == "Foul":
                m = _FOUL_RE.search(a.get("description", ""))
                if m:
                    fouls.append({
                        "gameId": g["gameId"], "pId": a["personId"], "name": a["playerName"],
                        "elapsed_s": elapsed, "period": a["period"], "personal_count": int(m.group(1)),
                    })
    return shots, fouls


def match_shots_to_stints(shots: list[dict], stints_with_times: list[dict]) -> None:
    """Mutates shots in place, adding since_checkin_s: how far into that
    player's current stint the shot was taken."""
    by_game_player = defaultdict(list)
    for s in stints_with_times:
        by_game_player[(s["gameId"], s["pId"])].append((s["in_s"], s["out_s"]))
    for shot in shots:
        hit = None
        for in_s, out_s in by_game_player.get((shot["gameId"], shot["pId"]), []):
            if in_s <= shot["elapsed_s"] <= out_s:
                hit = in_s
                break
        shot["since_checkin_s"] = None if hit is None else shot["elapsed_s"] - hit


def build_shot_quality_decay(shots: list[dict], min_fga: int = 30):
    matched = [s for s in shots if s["since_checkin_s"] is not None]
    lens_min = sorted(s["since_checkin_s"] / 60.0 for s in matched)
    n = len(lens_min)
    e1 = round(lens_min[int(33 / 100 * (n - 1))], 1)
    e2 = round(lens_min[int(66 / 100 * (n - 1))], 1)
    labels = [f"0-{e1:g} min", f"{e1:g}-{e2:g} min", f"{e2:g}+ min"]

    def bkt(m):
        if m < e1:
            return labels[0]
        if m < e2:
            return labels[1]
        return labels[2]

    def efg_stats(sub):
        fga = len(sub)
        if fga == 0:
            return None
        fgm = sum(1 for s in sub if s["made"])
        threes_made = sum(1 for s in sub if s["made"] and s["shotValue"] == 3)
        efg = (fgm + 0.5 * threes_made) / fga
        avg_dist = sum(s["shotDistance"] for s in sub) / fga
        return {"fga": fga, "fgm": fgm, "efg": round(efg, 3), "avg_dist": round(avg_dist, 1)}

    by_bucket = defaultdict(list)
    for s in matched:
        by_bucket[bkt(s["since_checkin_s"] / 60.0)].append(s)
    pooled = {b: efg_stats(by_bucket[b]) for b in labels}

    by_player_bucket = defaultdict(lambda: defaultdict(list))
    names, totals = {}, defaultdict(int)
    for s in matched:
        pid = s["pId"]
        by_player_bucket[pid][bkt(s["since_checkin_s"] / 60.0)].append(s)
        names[pid] = s["name"]
        totals[pid] += 1

    players = []
    for pid, tot in totals.items():
        if tot < min_fga:
            continue
        row = {"pId": pid, "name": names[pid], "fga": tot, "buckets": {}}
        for b in labels:
            st = efg_stats(by_player_bucket[pid][b])
            row["buckets"][b] = st if (st and st["fga"] >= 5) else None
        players.append(row)
    players.sort(key=lambda r: -r["fga"])

    return labels, pooled, players


def build_foul_trouble_cost(season: str, fouls: list[dict], stints_with_times: list[dict],
                              home_by_game: dict, foul_count_threshold: int = 2, period_cutoff: int = 1,
                              pull_window_s: float = 90.0):
    """For every instance of a player reaching `foul_count_threshold` personal
    fouls within `period_cutoff` periods, check whether the coach pulled them
    shortly after, and if so, what the team's net rating was during that
    forced-bench window versus checking back in."""
    by_game_player = defaultdict(list)
    for s in stints_with_times:
        by_game_player[(s["gameId"], s["pId"])].append(s)

    early = [f for f in fouls if f["personal_count"] == foul_count_threshold and f["period"] <= period_cutoff]
    results = []
    for f in early:
        stints = by_game_player.get((f["gameId"], f["pId"]), [])
        containing = [s for s in stints if s["in_s"] <= f["elapsed_s"] <= s["out_s"]]
        if not containing:
            continue
        stint = min(containing, key=lambda s: s["out_s"] - f["elapsed_s"])
        time_to_out = stint["out_s"] - f["elapsed_s"]
        if time_to_out > pull_window_s:
            continue  # coach kept them in -- not a forced-bench instance

        tl = build_score_timeline(season, f["gameId"])
        if tl is None:
            continue
        home = home_by_game[f["gameId"]]
        bench_start = stint["out_s"]
        later = sorted([s["in_s"] for s in stints if s["in_s"] > bench_start])
        bench_end = min(later[0], 1200.0) if later else 1200.0
        if bench_end <= bench_start:
            continue
        h0, a0 = score_at(tl, bench_start)
        h1, a1 = score_at(tl, bench_end)
        tempo0, opp0 = (h0, a0) if home else (a0, h0)
        tempo1, opp1 = (h1, a1) if home else (a1, h1)
        net = (tempo1 - opp1) - (tempo0 - opp0)
        minutes = (bench_end - bench_start) / 60.0
        results.append({
            "name": f["name"], "gameId": f["gameId"], "bench_minutes": round(minutes, 1),
            "net_pts": net, "per5min": round(net / minutes * 5, 2) if minutes > 0 else None,
        })

    total_min = sum(r["bench_minutes"] for r in results)
    total_net = sum(r["net_pts"] for r in results)
    pooled_per5 = round(total_net / total_min * 5, 2) if total_min > 0 else None
    return {"instances": results, "pooled": {"minutes": round(total_min, 1), "net_pts": total_net, "per5min": pooled_per5}}


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

    # team totals per bucket, for the leave-one-out baseline below
    team_bucket = defaultdict(lambda: {"minutes": 0.0, "net_pts": 0.0})
    for (pid, b), d in by_pb.items():
        team_bucket[b]["minutes"] += d["minutes"]
        team_bucket[b]["net_pts"] += d["net_pts"]

    qualified = [pid for pid, m in total_minutes.items() if m >= min_total_minutes]
    result = []
    for pid in qualified:
        row = {"pId": pid, "name": names[pid], "stints": stint_counts[pid],
               "total_minutes": round(total_minutes[pid], 1), "buckets": {}}
        for b in labels:
            d = by_pb.get((pid, b))
            if d and d["minutes"] >= 5:
                per5 = d["net_pts"] / d["minutes"] * 5
                # leave-one-out: team's own rate in this bucket, excluding this player's minutes
                tb = team_bucket[b]
                loo_min = tb["minutes"] - d["minutes"]
                loo_net = tb["net_pts"] - d["net_pts"]
                team_baseline = loo_net / loo_min * 5 if loo_min > 0 else None
                vs_team = round(per5 - team_baseline, 2) if team_baseline is not None else None
                row["buckets"][b] = {"per5min": round(per5, 2), "minutes": round(d["minutes"], 1), "stints": d["stints"],
                                      "vs_team": vs_team}
            else:
                row["buckets"][b] = None
        result.append(row)
    result.sort(key=lambda r: -r["stints"])
    return labels, result


def build_within_stint_decay(season: str, stints_with_times: list[dict], min_total_minutes: float = 40.0,
                              min_window_stints: int = 10, max_minute: int = 10, bin_s: float = 60.0):
    """The 3-bucket view above compares whole-stint averages -- it can't say
    WHEN within a stint a player's performance turns. This slices every
    stint into 1-minute windows (minute 0-1, 1-2, ...), scores each window
    against the play-by-play, and pools across every stint that lasted that
    long. Team-adjusted the same way as build_player_decay (leave-one-out),
    but now the adjustment is applied minute-by-minute instead of per bucket,
    so a player's own cliff point survives the adjustment instead of being
    smeared into one coarse number."""
    by_player_bin = defaultdict(lambda: {"net_pts": 0.0, "seconds": 0.0, "stints": 0})
    team_bin = defaultdict(lambda: {"net_pts": 0.0, "seconds": 0.0, "stints": 0})
    names, total_minutes = {}, defaultdict(float)

    for s in stints_with_times:
        tl = build_score_timeline(season, s["gameId"])
        if tl is None:
            continue
        home = s["home"]
        names[s["pId"]] = s["name"]
        total_minutes[s["pId"]] += s["elapsed_s"] / 60.0
        dur = s["out_s"] - s["in_s"]
        nbins = int(dur // bin_s) + (1 if dur % bin_s > 0 else 0)
        for i in range(min(nbins, max_minute)):
            w0 = s["in_s"] + i * bin_s
            w1 = min(s["in_s"] + (i + 1) * bin_s, s["out_s"])
            if w1 <= w0:
                continue
            h0, a0 = score_at(tl, w0)
            h1, a1 = score_at(tl, w1)
            t0, o0 = (h0, a0) if home else (a0, h0)
            t1, o1 = (h1, a1) if home else (a1, h1)
            net = (t1 - o1) - (t0 - o0)
            d = by_player_bin[(s["pId"], i)]
            d["net_pts"] += net; d["seconds"] += (w1 - w0); d["stints"] += 1
            td = team_bin[i]
            td["net_pts"] += net; td["seconds"] += (w1 - w0); td["stints"] += 1

    team_curve = []
    for i in range(max_minute):
        td = team_bin[i]
        rate = round(td["net_pts"] / td["seconds"] * 300, 2) if td["seconds"] > 0 else None
        team_curve.append({"minute": i, "per5min": rate, "minutes": round(td["seconds"] / 60.0, 1), "stints": td["stints"]})

    qualified = [pid for pid, m in total_minutes.items() if m >= min_total_minutes]
    players = []
    for pid in qualified:
        points = []
        for i in range(max_minute):
            d = by_player_bin.get((pid, i))
            td = team_bin[i]
            if not d or d["seconds"] < 15:
                break  # this player never reliably reaches this minute of a stint
            rate = d["net_pts"] / d["seconds"] * 300
            loo_sec = td["seconds"] - d["seconds"]
            loo_net = td["net_pts"] - d["net_pts"]
            loo_rate = loo_net / loo_sec * 300 if loo_sec > 0 else 0.0
            points.append({
                "minute": i, "raw": round(rate, 2), "vs_team": round(rate - loo_rate, 2),
                "minutes": round(d["seconds"] / 60.0, 1), "stints": d["stints"],
                "reliable": d["stints"] >= min_window_stints,
            })
        if points:
            players.append({"pId": pid, "name": names[pid], "points": points})
    players.sort(key=lambda p: -total_minutes[p["pId"]])
    return team_curve, players


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

def build_lineup_segments(season: str, stints_with_times: list[dict]) -> tuple[list[dict], dict]:
    """Every maximal continuous run of a given 5-man unit (bounded by real
    substitutions -- each boundary in the sweep is an actual Tempo sub, so
    each segment IS one continuous lineup stint, not an arbitrary slice)."""
    by_game = defaultdict(list)
    for s in stints_with_times:
        by_game[s["gameId"]].append(s)
    names = {s["pId"]: s["name"] for s in stints_with_times}

    segments = []
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
            segments.append({"key": tuple(sorted(on_court)), "length_s": t1 - t0, "net_pts": net})
    return segments, names


def build_lineups(segments: list[dict], names: dict, min_minutes: float = 15.0) -> list[dict]:
    lineup_agg = defaultdict(lambda: {"seconds": 0.0, "net_pts": 0.0, "segments": 0})
    for s in segments:
        d = lineup_agg[s["key"]]
        d["seconds"] += s["length_s"]
        d["net_pts"] += s["net_pts"]
        d["segments"] += 1

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


def build_lineup_decay(segments: list[dict], names: dict, top_n: int = 6, min_lineup_segments: int = 10):
    """Does a 5-man unit's own performance decay the longer that exact
    lineup has been on the floor together, continuously (separate question
    from individual player stint length)."""
    lens_min = sorted(s["length_s"] / 60.0 for s in segments)
    n = len(lens_min)
    e1 = round(lens_min[int(33 / 100 * (n - 1))], 2)
    e2 = round(lens_min[int(66 / 100 * (n - 1))], 2)
    labels = [f"0-{e1:g} min", f"{e1:g}-{e2:g} min", f"{e2:g}+ min"]

    def bucketed(seglist):
        by_bucket = defaultdict(lambda: {"minutes": 0.0, "net_pts": 0.0, "stints": 0})
        for s in seglist:
            m = s["length_s"] / 60.0
            b = bucket_for(m, e1, e2, labels)
            by_bucket[b]["minutes"] += m
            by_bucket[b]["net_pts"] += s["net_pts"]
            by_bucket[b]["stints"] += 1
        out = {}
        for b in labels:
            d = by_bucket[b]
            out[b] = None if d["minutes"] < 1 else {"per5min": round(d["net_pts"] / d["minutes"] * 5, 2),
                                                       "minutes": round(d["minutes"], 1), "stints": d["stints"]}
        return out

    pooled = bucketed(segments)

    by_lineup = defaultdict(list)
    for s in segments:
        by_lineup[s["key"]].append(s)
    top = sorted(by_lineup.items(), key=lambda kv: -sum(x["length_s"] for x in kv[1]))
    per_lineup = []
    for key, seglist in top:
        if len(seglist) < min_lineup_segments:
            continue
        if len(per_lineup) >= top_n:
            break
        per_lineup.append({
            "names": [names[p] for p in key],
            "total_minutes": round(sum(x["length_s"] for x in seglist) / 60.0, 1),
            "runs": len(seglist),
            "buckets": bucketed(seglist),
        })

    return labels, pooled, per_lineup


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

def render(template_path: Path, out_path: Path, *, lineups,
           shot_buckets, shot_decay_pooled, shot_decay_players, within_stint_team, within_stint_players):
    tpl = template_path.read_text()
    tpl = tpl.replace("__LINEUPS_JSON__", json.dumps(lineups))
    tpl = tpl.replace("__SHOT_BUCKETS_JSON__", json.dumps(shot_buckets))
    tpl = tpl.replace("__SHOT_DECAY_POOLED_JSON__", json.dumps(shot_decay_pooled))
    tpl = tpl.replace("__SHOT_DECAY_PLAYERS_JSON__", json.dumps(shot_decay_players))
    tpl = tpl.replace("__WITHIN_STINT_TEAM_JSON__", json.dumps(within_stint_team))
    tpl = tpl.replace("__WITHIN_STINT_PLAYERS_JSON__", json.dumps(within_stint_players))
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
    schedule = load_schedule(args.season)
    games, wins, losses = team_games_and_record(schedule, args.team_id)
    print(f"{len(games)} non-preseason final games ({wins}-{losses})")

    print("Fetching gamerotation data (stint boundaries)...")
    stints = fetch_gamerotation_stints(args.season, args.team_id, games, with_raw_times=True)
    print(f"{len(stints)} stints extracted")

    print("Reconstructing 5-man lineups...")
    lineup_segments, lineup_names = build_lineup_segments(args.season, stints)
    lineups = build_lineups(lineup_segments, lineup_names)
    print(f"  {len(lineups)} lineups with >=15 min, {len(lineup_segments)} continuous-run segments total")

    print("Fetching shot events...")
    shots, _fouls = fetch_shots_and_fouls(args.season, args.team_id, games)
    match_shots_to_stints(shots, stints)
    shot_buckets, shot_decay_pooled, shot_decay_players = build_shot_quality_decay(shots)
    print(f"  {len(shots)} shot attempts, edges: {shot_buckets}")

    print("Building within-stint (minute-by-minute) decay curves...")
    within_stint_team, within_stint_players = build_within_stint_decay(args.season, stints)
    print(f"  {len(within_stint_players)} players with a curve")

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stints.json").write_text(json.dumps(stints))
    (data_dir / "lineups.json").write_text(json.dumps(lineups, indent=2))
    (data_dir / "shot_quality_decay.json").write_text(json.dumps(
        {"bucket_order": shot_buckets, "pooled": shot_decay_pooled, "players": shot_decay_players}, indent=2))
    (data_dir / "within_stint_decay.json").write_text(json.dumps(
        {"team": within_stint_team, "players": within_stint_players}, indent=2))
    print(f"Wrote data files to {data_dir}/")

    render(
        Path(args.template), Path(args.out),
        lineups=lineups,
        shot_buckets=shot_buckets, shot_decay_pooled=shot_decay_pooled, shot_decay_players=shot_decay_players,
        within_stint_team=within_stint_team, within_stint_players=within_stint_players,
    )
    print(f"Rendered {args.out}")


if __name__ == "__main__":
    main()
