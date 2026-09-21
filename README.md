# Toronto Tempo Analysis

Three things, for the Toronto Tempo (2026 WNBA expansion franchise, team ID `1611661332`):

1. **Shot-quality decay** — a player's own eFG% and shot distance by how long they'd been in their current stint. Individually attributable, unlike net rating (which is confounded by the other 9 players on the floor).
2. **Cliff-point finder** — every stint sliced minute-by-minute (not classified by its final length), team-adjusted at each minute (leave-one-out), so each player's own performance line — and exactly where it turns — survives the adjustment.
3. **5-man lineup net ratings** — reconstructed from on/off overlap, scored against the play-by-play.

A few other angles (net rating by stint-length bucket, score-margin control, league benchmark, lineup decay, foul-trouble cost) were tried and cut — they either repeated the same confounded "coaches sub players out when things go wrong" story, or didn't hold up on a small sample. See git history if you want them back.

## Data source

`stats.wnba.com` blocks requests from cloud/datacenter IPs (including GitHub Actions runners), so this doesn't hit the live API. Instead it pulls from [`sportsdataverse/wehoop-wnba-stats-raw`](https://github.com/sportsdataverse/wehoop-wnba-stats-raw), a public repo that mirrors raw stats.wnba.com payloads, updated through the current season — both the `gamerotation` endpoint (exact stint check-in/check-out/point-diff) and `playbyplayv3` (shot-by-shot detail, running score).

## Build

```
python3 build_tempo_analysis.py --team-id 1611661332 --season 2026 --template template.html --out docs/index.html --data-dir data
```

Writes:
- `data/stints.json` — every individual stint (raw)
- `data/shot_quality_decay.json`, `data/within_stint_decay.json`, `data/lineups.json` — the three analyses above
- `docs/index.html` — the rendered dashboard (served via GitHub Pages if enabled on this repo)

The `.github/workflows/update-tempo-analysis.yml` workflow runs this weekly and on demand (`workflow_dispatch`), committing the refreshed data and dashboard.
