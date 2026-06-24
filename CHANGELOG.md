# Changelog

## v0.1.0-public-beta

Initial public beta release.

### Added

- Explainable YouTube niche scoring across demand, supply, monetization, quality, Trends,
  comments, confidence, and query consensus.
- Multi-query sampling to reduce single-search noise.
- Topic dedupe and lightweight clustering for near-duplicate niches.
- Authority concentration scoring for search pages dominated by a few channels.
- Optional Google Trends YouTube Search signal with baseline comparison.
- Optional external keyword/RPM CSV import.
- Offline demo mode: `youtube-niche --demo`.
- Retrospective proxy backtest harness and forward-test snapshot registry.
- Cache-only mode for reproducible/no-quota runs.
- Open-source community files, issue templates, CI, and contribution guide.

### Known Limits

- YouTube does not expose true search volume through the public API.
- Retrospective backtests use current public view/subscriber counts because historical snapshots
  are unavailable through the API.
- Google Trends access uses `pytrends`, which is unofficial and rate-limit prone.
- Scores are directional evidence, not guarantees that a channel will rank or grow.
