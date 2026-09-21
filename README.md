# Toronto Tempo Analysis

Rotation/fatigue-decay analysis for the Toronto Tempo (2026 WNBA expansion franchise, team ID `1611661332`): net rating segmented by elapsed time within each on-court stint, with bucket edges set from the real stint-length distribution (terciles) rather than guessed.

## Data source

`stats.wnba.com` blocks requests from cloud/datacenter IPs (including GitHub Actions runners), so this doesn't hit the live API. Instead it pulls from [`sportsdataverse/wehoop-wnba-stats-raw`](https://github.com/sportsdataverse/wehoop-wnba-stats-raw), a public repo that mirrors raw stats.wnba.com payloads, updated through the current season. Specifically the `gamerotation` endpoint, which records every continuous on-court stint directly (check-in time, check-out time, point differential across that window) — no play-by-play reconstruction needed.

## Build

```
python3 build_tempo_analysis.py --team-id 1611661332 --season 2026 --template template.html --out docs/index.html --data-dir data
```

Writes:
- `data/stints.json` — every individual stint (raw)
- `data/player_decay.json` — bucketed per-player net rating
- `docs/index.html` — the rendered dashboard (served via GitHub Pages if enabled on this repo)

The `.github/workflows/update-tempo-analysis.yml` workflow runs this weekly and on demand (`workflow_dispatch`), committing the refreshed data and dashboard.
