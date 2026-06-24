# Roadmap

YouTube Niche Finder is an evidence engine, not a magic oracle. The roadmap is organized around
making the recommendations harder to fool and easier to validate.

## Now

- Multi-query consensus scoring so one noisy search page cannot dominate a niche.
- Topic clustering and deduping so near-identical niches do not crowd the top ranks.
- Authority concentration scoring for search pages dominated by a few channels.
- Offline demo mode for first-run users without API keys.
- Backtest and forward-test registries for accumulating validation evidence.
- Public beta feedback loop through Evaluation feedback issues.

## Next

- Import optional external keyword-volume and RPM/CPM CSVs.
- Add anonymized example reports from real runs once private details are removed.
- Expand cache-only workflows so contributors can reproduce reports from shared fixtures.
- Improve semantic relevance matching beyond lexical title overlap.
- Publish aggregate validation results once enough backtest and forward-test runs exist.

## Later

- Optional integrations with paid keyword tools.
- Richer per-domain baselines for Trends and view-velocity knees.
- Lightweight web UI for comparing opportunities and validation runs.
- Versioned releases on PyPI after the CLI and report schema stabilize.
