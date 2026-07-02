# Changelog

## 2026-07-01

A 2026-06-30 incident (one LLM backend went silently empty for a day, degrading niche extraction
and writing a thin label into the live forward-test ledger without anyone noticing) motivated most
of this batch: two reliability hardening passes, plus report-quality and compliance work queued up
alongside it.

### Added

- **LLM failover chain** (`llm.py`): on an empty response, the configured/primary LLM backend now
  fails over through the remaining available providers in a fixed order (`agy` → `codex` → `claude`
  → `grok` → `anthropic`). Every hop prints a `[llm]` warning so a degraded run is loud instead of
  silent. Set `LLM_FALLBACK=0` to disable the chain and use only the configured primary. When every
  provider in the chain still comes back empty, niche extraction (`winners.py`) falls back to
  keyword n-grams and marks the run's `extraction_method` as `keyword_degraded`.
- **Forward-ledger hardening** (`forward.py`): every write to `out/forward-snapshots.csv` now takes
  a timestamped backup to `out/backups/` first (pruned to the newest 30), records an
  `extraction_method` provenance column (`llm` / `keyword` / `keyword_degraded`, migrated in place
  on existing ledgers), and runs a junk-label gate that rejects thin title-fragment labels before
  they reach the ledger. A fully degraded extraction run (`keyword_degraded`) skips snapshotting
  entirely rather than polluting the ledger with generic keyword n-grams.
- **Google Trends SerpApi fallback** (`signals/trends.py`): `pytrends` is unofficial and its
  upstream repo is dead (archived April 2025, chronic 429s). When it fails and `SERPAPI_KEY` is
  set, both the 12-month momentum signal and the 5-year durability signal now fall back to SerpApi's
  `google_trends` engine (plain HTTP, no new dependency). See `.env.example` for the free-tier
  signup note.
- **ToS retention scrubber** (`youtube_niche/retention.py`, new): `python -m youtube_niche.retention`
  enforces the YouTube API Developer Policies' ~30-day raw-data retention limit against everything
  this tool writes to disk. Dry-run by default; `--apply` deletes raw evidence CSVs older than 30
  days, row-level-scrubs the `views`/`subs`/`views_per_day` columns from `evidence-snapshots*.csv`
  ledger rows past the window (rows and derived scores are preserved), and purges stale cache rows.
  `out/forward-snapshots.csv` and `out/backups/` are never touched.
- **Report-only `product_fit` axis** (`monetization.py`, `domains.py`, `cli.py`): scores how well a
  niche supports selling the creator's OWN products (high-ticket coaching/courses > digital
  products > affiliate > AdSense-only), distinct from ad-CPM monetization. Per-domain base fit plus
  topic-level commercial/service/free-intent keyword nudges. Report-only — never blended into
  `opportunity` or any other existing score.
- **Report enrichments** (new `formats.py`; `winners.py`/`report.py`/`llm.py`): per-niche
  `dominant_format` (listicle/explainer/story/news, classified from breakout titles),
  `replication_channels` (count of distinct small channels independently breaking out on the same
  theme — 3+ is the strongest replicability signal available here), a `positioning` readout
  (Learner-viable / Enthusiast / Expert-required, from the niche's own newcomer-volume/small-share/
  authority-concentration metrics), and an LLM-generated "I help [X] do/overcome [Y]" positioning
  hypothesis for the top 5 niches per run. Five new CSV columns: `product_fit`, `positioning`,
  `dominant_format`, `replication_channels`, `hypothesis`.
- **`niche-coach` skill** (`.claude/skills/niche-coach/`): a data-grounded niche-selection coach
  that interviews for background/skills/interests, then validates candidate niches against this
  repo's live demand/supply pipeline instead of asserting demand from priors.
- **Wayback Machine backfill probe** (`youtube_niche/wayback.py`, new, experimental): queries the
  Internet Archive's free CDX index for archived YouTube channel/watch pages and parses the exact
  `subscriberCount`/`viewCount` out of embedded `ytInitialData` JSON, as a leakage-free retrospective
  backfill source for channels the Archive happened to snapshot. ToS-advantaged (Internet Archive
  data is not YouTube API data, so the 30-day retention rule does not apply to it), but coverage is
  power-law by fame — see the module docstring for the empirical hit-rate on small channels.

 a new long-run check separates durable veins
  from one-week flashes — `trends_durability` scores whether a niche sits on a structurally rising
  multi-year base (recent-year vs early-year YouTube-search interest). Surfaced as a `📈 durable` /
  `⚠️ fading` flag in `winners` output and as columns in the CSV/MD reports. Independent of the
  12-month momentum signal (`--no-trends` runs still compute it; disable with `--no-durability`),
  cached 30 days, and forced off in backtests (a slope-to-today is look-ahead for a past holdout).

- **Off-domain breakout filter**: breakouts are now filtered by YouTube category, dropping Gaming /
  Music / Sports / Film / Autos videos whose finance keywords would otherwise mint bogus niches
  (e.g. an in-game "money trick" video). Safe globally — every mined domain is money/info/educational.

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
