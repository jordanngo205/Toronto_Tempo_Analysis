# Toronto Tempo Analysis

WNBA play-by-play scraping and rotation/fatigue-decay analysis for the Toronto Tempo (2026 expansion franchise, team ID `1611661332`).

Same methodology as the Canada senior women's national team rotation-decay analysis: reconstruct on-court stints from play-by-play substitution events, then look at team net rating segmented by elapsed time within each stint, with per-bucket stint counts as a reliability signal.

## Scraping

`wnba_scrape.py` pulls team game logs, play-by-play, and box scores from the `stats.wnba.com` API and writes:

- `<name>/data/<name> - game details.csv`
- `<name>/data/<name> - pbp.csv`
- `<name>/data/<name> - player box scores.csv`

```
python3 wnba_scrape.py --team-id 1611661332 --team-name "Toronto Tempo" --season 2026 --name "Toronto Tempo 2026"
```

The `.github/workflows/scrape-wnba-tempo.yml` workflow runs this on demand (`workflow_dispatch`) and commits the resulting data.
