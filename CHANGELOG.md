# Changelog

## Unreleased

### Added

- **Winners-first → stage-2 loop**: `winners --emit-subtopics` writes breakout-derived niches to
  `discovered_subtopics.json`; `--from-domain` now prefers these data-derived seeds over the
  hand-curated list (falls back to curated, reports `source:`).
- **Community calibration**: `youtube_niche.community calibrate` pools resolved forward-test
  snapshots into a score-vs-reality AUC curve; `validate` checks contributions. See `community/`.
- **Forward-test `resolve`**: closes the forward loop by checking due snapshots against real
  breakouts and marking hit/miss.
- **Keyless fixtures**: `backtest --fixtures` runs the full pipeline with no API key or quota.
- **Backtest precision split by candidate source** (`subtopic` = clean/non-circular vs
  `holdout_label` = circular), surfaced in the aggregate.

### Validation

- First clean (`--candidate-source subtopics`) backtest across finance/AI/business returned ~0%
  precision: curated subtopic lists do not match real breakouts (confirmed uncapped, offline). This
  motivated the winners-first → stage-2 loop above.

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
