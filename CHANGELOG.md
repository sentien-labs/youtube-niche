# Changelog

## Unreleased

### Added

- **Winners-first → stage-2 loop**: `winners --emit-subtopics` writes breakout-derived niches to
  a writable user registry; `--from-domain` reads shipped discovered seeds plus that user overlay
  before falling back to the hand-curated list (reports `source:`).
- **Community calibration**: `youtube_niche.community calibrate` pools resolved forward-test
  snapshots into a score-vs-reality AUC curve; `validate` checks contributions. See `community/`.
- **Forward-test `resolve`**: closes the forward loop by checking due snapshots against real
  breakouts and marking hit/miss.
- **Keyless fixtures**: `backtest --fixtures` runs the full pipeline with no API key or quota.
- **Backtest precision split by candidate source** (`subtopic` = clean/non-circular vs
  `holdout_label` = circular), surfaced in the aggregate.
- **Backtest `--candidate-source effective`**: replays the actual `--from-domain` seed source
  (discovered registry when present, curated fallback) and reports discovered-subtopic metrics
  separately from the clean curated baseline.
- **Backtest `--candidate-source temporal`**: mines winners-first seed topics from a pre-holdout
  window, freezes those candidates, scores with pre-holdout searches, and tests against the later
  holdout.
- **Grok CLI LLM backend**: `--llm-provider grok` / `LLM_PROVIDER=grok` can use an already
  authenticated `grok` CLI for niche extraction, comment-demand, and quality-depth signals.
- **Grok model pinning**: `GROK_MODEL` pins the Grok CLI model for repeatable runs, with
  `GROK_COMMENT_MODEL` / `GROK_QUALITY_MODEL` available for tier-specific A/B tests.
- **Grok/X boundary documented**: Grok CLI is treated as an LLM reasoning backend only; native
  X/Twitter demand should be added later as a separate API-backed signal, not inferred from the CLI.
- **Offline backtest failure audit**: `python -m youtube_niche.audit` reads existing backtest
  reports with no quota, compares missed breakouts against curated/discovered seeds, and surfaces
  whether failures are mostly seed coverage, matching, or ranking/scoring.
- **Shared relevance normalization**: live supply scoring and backtest matching now share simple
  normalization for plural/singular, local/locally, cancel spelling, retire/retiring, business,
  sell/selling, and invest/investing matches, with stricter checks for broad partial overlaps.

### Validation

- First clean (`--candidate-source subtopics`) backtest across finance/AI/business returned ~0%
  precision in the sampled holdout: curated subtopic lists did not match the observed breakouts.
  This motivated the winners-first → stage-2 loop above; prospective forward tests are still the
  evidence needed before claiming predictive lift.

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
