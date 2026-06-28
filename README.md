# YouTube Niche Finder

[![CI](https://github.com/sentien-labs/youtube-niche/actions/workflows/ci.yml/badge.svg)](https://github.com/sentien-labs/youtube-niche/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Find **high-demand, high-monetization, low-supply** YouTube topics. Give it a niche; it expands
to candidate topics, pulls data from the YouTube Data API, scores each on demand, monetization,
supply, content depth, confidence, and query consensus, then writes an explainable CSV + Markdown
report.

The guiding principle: a real opportunity needs demand, monetization, and thin supply at the
same time. High demand + high supply = saturated. Low demand + low supply = usually nobody
cares. High CPM + no demand = not enough. The score gates low view velocity and shows confidence
so missing evidence cannot quietly look like certainty.

## Sample Output

Want to see the report shape before installing? Open the synthetic
[`examples/demo-report.md`](examples/demo-report.md). It shows opportunity, demand, monetization,
low-supply, thin-content, confidence, query consensus, watch-outs, and evidence notes in the same
format as the offline demo.

## Quick Evaluation

The easiest way to help is to run the tool on a niche you already understand and tell us where
the ranking feels right or wrong.

### 1. Try The Offline Demo

No API keys needed:

```bash
git clone https://github.com/sentien-labs/youtube-niche.git
cd youtube-niche
python3 -m venv venv
source venv/bin/activate
pip install -e .
youtube-niche --demo
```

If the console command is not on your PATH, use `python -m youtube_niche --demo`.

Open the generated Markdown report in `out/`. The demo numbers are synthetic, but the report
format, caveats, and confidence fields match live runs.

### 2. Evaluate A Real Niche

Add a YouTube Data API key to `.env`:

```bash
cp .env.example .env
# edit .env and set YOUTUBE_API_KEY
```

Then run a small, cheap scan:

```bash
youtube-niche "your niche here" --query-samples 3 --top-n 20 --no-llm
```

For example:

```bash
youtube-niche "off grid solar for vans" --query-samples 3 --top-n 20 --no-llm
```

Look at the top 5 topics in the Markdown report. The most useful feedback is not “good” or
“bad”; it is specific:

- Which top-ranked niches look genuinely underserved?
- Which rankings are wrong, saturated, too broad, or too low-demand?
- Which YouTube results prove your point?
- Which signal seems responsible: demand, relevance, competition, Trends, CPM, comments, or confidence?

Open an [Evaluation feedback issue](../../issues/new?template=evaluation_feedback.md) and paste
the command you ran, the top results, and what your domain knowledge says. Anonymized snippets are
fine; do not upload private cache databases, API keys, or client research.

### 3. Improve The Engine

Good first contributions include:

- testing the scorer on niches you know well;
- adding better relevance-matching test cases;
- adding domain/subtopic ideas with realistic CPM notes;
- improving backtest and forward-test summaries;
- contributing anonymized example reports;
- wiring optional external keyword-volume or RPM data.

## Signals

| # | Signal | Source | What it measures |
|---|--------|--------|------------------|
| A | **Outlier** | videos + channels | views ÷ subscribers-at-publish (estimated) — topic-carried hits = beatability/portability context |
| B | **Supply age** | search `publishedAt` | stale top results = abandoned demand |
| C | **Competition** | search results | few credible videos + low authority concentration = thin supply |
| D | **Small channels** | channels | successful videos from channels estimated small at publish = beatable supply |
| E | **Comment demand** | commentThreads + LLM | "please make a video on X" — literal unmet demand |
| F | **Trends** | Google Trends (YouTube Search source) | rising interest + breakout queries (12-month momentum) |
| F2 | **Durability** | Google Trends (5-year) | structurally rising base (`📈 durable`) vs fading flash (`⚠️ fading`) |
| G | **Content depth** | transcripts + LLM | how thin the top-ranking videos actually are |
| H | **Monetization** | curated CPM + keyword intent | domain CPM and advertiser intent proxy |
| H2 | **External metrics** | optional CSV | imported search-volume/RPM evidence |
| I | **Relevance gate** | title match | caps demand when search results do not clearly match the niche |
| J | **Confidence** | signal coverage | how complete the evidence is |

There is intentionally no X/Twitter signal today. `LLM_PROVIDER=grok` uses the local Grok CLI as
an LLM for reasoning/extraction only; native X data would require a separate xAI API `x_search`
integration and should be validated as its own demand signal.

`opportunity = confidence × demand_gate × geomean(demand, low-supply, monetization, thin-content)`.
The demand gate includes both view velocity and a relevance gate, so unrelated fuzzy search
results cannot create a fake "high demand / low supply" opportunity.
The raw opportunity and confidence-adjusted opportunity are both shown. Optional missing signals
lower confidence instead of being silently treated as strong evidence.

Use `--query-samples 3` for more robust scoring. The scorer evaluates several query phrasings,
uses median signal values across relevant samples, and lowers confidence when query coverage or
agreement is weak.

Confidence is evidence coverage, not a calibrated win probability. Backtests default to disabling
Trends, comments, and LLM depth scoring to reduce leakage, so their confidence scores are expected
to be capped lower. For live research, use `--query-samples 3` plus an authenticated LLM provider
such as `--llm-provider grok` when you want the fuller evidence stack.

## Setup For Live Runs

```bash
cd youtube-niche
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env   # then fill in your keys
```

`.env`:

```
# YouTube auth — an API key, OR OAuth client-secret + token paths (see .env.example)
YOUTUBE_API_KEY=...

# LLM for signals E (comments) and G (depth). 'auto' uses the anthropic SDK if a key is
# set, else the codex CLI. Or pick: codex | claude | agy | grok (CLIs use their own auth) | anthropic.
LLM_PROVIDER=codex
# Note: LLM_PROVIDER=grok is a Grok CLI reasoning backend, not native X/Twitter data access.
# Optional when LLM_PROVIDER=grok. GROK_MODEL applies to both tiers; tier overrides win.
# GROK_MODEL=grok-composer-2.5-fast
# GROK_QUALITY_MODEL=grok-build
# ANTHROPIC_API_KEY=...   # only if LLM_PROVIDER=anthropic

# Optional locale/quota tuning
# YOUTUBE_REGION_CODE=US
# YOUTUBE_RELEVANCE_LANGUAGE=en
# TRENDS_GEO=US
# YOUTUBE_DAILY_SEARCH_LIMIT=100
# KEYWORD_METRICS_CSV=/absolute/path/to/keyword-metrics.csv
```

### LLM provider

Signals E and G call an LLM. Rather than require an API key, the tool can shell out to an
already-authenticated CLI:

| Provider | Invocation | Notes |
|----------|-----------|-------|
| `codex` | `codex exec` | OpenAI; clean output via `-o`. **Verified working.** |
| `claude` | `claude -p` | Anthropic CLI (needs the CLI logged in) |
| `agy` | `agy -p` | Google/Gemini CLI (needs `agy` signed in) |
| `grok` | `grok -p` | xAI/Grok CLI (needs `grok` signed in) |
| `anthropic` | SDK | needs `ANTHROPIC_API_KEY` |

Pick with `--llm-provider` or `LLM_PROVIDER`. `auto` = anthropic key if present, else codex.
For Grok, set `GROK_MODEL` to pin the CLI model instead of relying on the local default. The
lightweight default to test first is `grok-composer-2.5-fast`; `grok-build` is worth comparing on
the quality/niche-extraction tier via `GROK_QUALITY_MODEL`.

Grok CLI is not treated as an X-data source. Use it for second-opinion reasoning, niche extraction,
comment-demand classification, and transcript-depth scoring. If the project adds X momentum later,
that should live as a separate API-backed signal using xAI `x_search` or another auditable X data
provider, with its own raw evidence and calibration.

Note: CLI backends spawn one subprocess per call (~5–20s each), so a large run with depth
scoring takes a while — keep `--max-seeds` modest while tuning.

## Usage

Two stages. **Stage 1** finds which high-CPM *domain* has the best demand/supply gap; **stage 2**
drills into a domain to find the specific underserved *niche*.

```bash
# Stage 1 — rank curated high-CPM domains (finance, insurance, crypto, AI, …)
python -m youtube_niche.discover --terms 3
python -m youtube_niche.discover --domains insurance,crypto --terms 4   # subset

# Stage 2 — find the niche inside the winning domain
python -m youtube_niche "off-grid solar for vans"
python -m youtube_niche --from-domain "personal finance"   # hybrid: discovered + autocomplete + curated

# Winners-first — discover niches FROM proven small-at-publish breakouts (not a guessed list).
# It mines domain probes plus autocomplete-expanded probes, then writes data-derived seeds.
python -m youtube_niche.winners --domain "personal finance" --emit-subtopics

# Backtest — check whether high-ranked candidates match later small-at-publish breakouts
python -m youtube_niche.backtest --domain "AI" --query-samples 2 --max-candidates 10
python -m youtube_niche.backtest --domain "AI" --candidate-source effective   # replay --from-domain seeds
python -m youtube_niche.backtest --domain "AI" --candidate-source hybrid      # replay new hybrid source mix
python -m youtube_niche.backtest --domain "AI" --candidate-source temporal --cutoff 2026-01-01
python -m youtube_niche.benchmark --domain "AI" --windows 4 --candidate-source temporal --calibrate-weights
python -m youtube_niche.backtest --aggregate
python -m youtube_niche.weighting --registry out/backtest-runs.csv
python -m youtube_niche.audit --out-dir out   # no-quota audit of existing backtest miss reports

# Forward-test — save today's scored topics for 30/60/90-day follow-up
python -m youtube_niche "off-grid solar for vans" --snapshot
python -m youtube_niche.forward summary
python -m youtube_niche.forward resolve
```

The domain list and its (industry-estimate) CPM ranges live in `youtube_niche/domains.py` —
edit freely. CPM is *not* available from the YouTube API; the registry is the curated input.

**Winners-first reduces reliance on guessed subtopics.** The first clean backtests showed the
hand-curated `domain.subtopics` missing this holdout's real small-at-publish breakouts, often because
the curated lists skew toward niche minutiae while demand concentrates on broader themes.
`winners --emit-subtopics` closes the loop: it mines real breakouts, reads the niches off them, and
records them in a writable user registry. A breakout counts channels that were small *when they
published* (subscribers estimated by prorating current subs over channel age), so a channel that
broke out and then grew past the small-channel cap is still recognized as a winner instead of being
filtered out. Winners-first broadens its breakout search from the domain's hand-written probes with
YouTube autocomplete by default; use `--no-probe-autocomplete` for a stricter audit run. `--from-domain` defaults to a hybrid candidate list: discovered
winners-first niches first, then YouTube autocomplete expansions from the domain probes, then the
curated fallback list. Use `--candidate-mode expanded` to audit autocomplete + curated coverage
without discovered topics, `--candidate-mode effective` for the older behavior (discovered if
present, otherwise curated), or `--candidate-mode curated` / `--candidate-mode discovered` for
source-specific audits.

Scored reports also write sidecar CSVs ending in `-video-evidence.csv` and
`-channel-evidence.csv`. These preserve the sampled videos/channels behind each topic score:
views/day, current subscribers, estimated subscribers-at-publish, publish-time and current
views/subscriber ratios, title relevance, small-at-publish breakout role, and URLs. They include
category-wide ranks so you can inspect the strongest video and channel proof across the whole
report, not just inside one topic. The rank is based on a combined opportunity-evidence score:
topic opportunity multiplied by the sampled video's or channel's proof strength.
Reports also append the best proof rows to `out/evidence-snapshots.csv` with `pending` status, so
the exact videos/channels that justified a category can be checked later instead of only the
topic-level forecast.

Useful flags:

| Flag | Effect |
|------|--------|
| `--max-seeds N` | cap candidate topics (default 20) |
| `--candidate-mode MODE` | for `--from-domain`: `hybrid` default, or `expanded`, `effective`, `curated`, `discovered` |
| `--max-probe-terms N` | for `winners`/`backtest`: cap autocomplete-expanded breakout mining probes (default 15) |
| `--no-probe-autocomplete` | for `winners`/`backtest`: mine breakouts only from hand-written domain probes |
| `--top-n N` | search results scanned per seed (default 30) |
| `--query-samples N` | search-query variants per topic; use 3 to reduce single-search noise |
| `--alphabet-soup` | aggressive autocomplete expansion (more seeds) |
| `--no-llm` | skip comment + depth signals (no Anthropic key needed) |
| `--no-trends` | skip the 12-month Google Trends momentum signal (faster, avoids rate-limits) |
| `--no-durability` | skip the 5-year Trends durability check (otherwise runs even under `--no-trends`) |
| `--quota-budget N` | override the daily unit budget |
| `--search-limit N` | override the daily `search.list` call budget |
| `--region-code CC` | YouTube/autocomplete region (default US) |
| `--relevance-language xx` | YouTube relevance language (default en) |
| `--trends-geo CC` | Google Trends geo (default US) |
| `--metrics-csv PATH` | optional external keyword/RPM metrics CSV |
| `--cache-only` | use cached YouTube responses only; never spend API quota |
| `--snapshot` | append scored topics to the forward-test snapshot registry |

Outputs land in `./out/<slug>-<timestamp>.{csv,md}`.

### Backtesting

`python -m youtube_niche.backtest` runs a retrospective proxy backtest:

1. mine small-at-publish breakout videos in a holdout window;
2. score candidate niches using searches restricted to videos published before that holdout;
3. report whether high-ranked candidates matched the later breakout videos.

This is directional, not a perfect historical replay. The YouTube Data API does not provide
historical view/subscriber snapshots, so pre-holdout videos still carry current public counts.
To avoid the worst distortion, view velocity is measured against the real wall-clock (current
views ÷ current age = a consistent lifetime-average) rather than dividing current views by the
shorter pre-holdout window, which would inflate videos published just before the holdout; a
milder, non-inflationary leak remains. By default the harness disables comments, LLM quality,
and Trends to reduce future leakage; add `--with-comments`, `--with-llm`, or `--with-trends` when
you intentionally want those signals. The leakage-free measure is the forward test (below).
Backtest runs append to `out/backtest-runs.csv`; use `python -m youtube_niche.backtest --aggregate`
to generate a cross-run validation summary.

For an 8+/10 validation process, prefer the multi-window runner:

```bash
python -m youtube_niche.benchmark --domain "AI" --windows 4 --candidate-source temporal --calibrate-weights
```

This repeats holdout windows, writes a benchmark manifest, aggregates source-sliced precision and
recall, and optionally emits `weight-calibration-*.{csv,md}`. The weight report grid-searches
conservative demand/supply/monetization mixes against actual backtest hits, then reports the best
AUC and top-quartile lift. Treat the recommendation as evidence for a config change, not an
automatic mutation of defaults.

**Read the `subtopic` numbers, not the headline.** Metrics are split by candidate source.
`subtopic` candidates are curated topics *not* derived from the holdout breakouts — the clean
baseline. `holdout_label` candidates are read off the breakout titles, so they hit almost by
construction and are flagged circular. `--candidate-source effective` replays the actual
`--from-domain` seed source, including discovered subtopics when present; treat that as clean only
when the discovered registry was generated before the tested holdout window.
`--candidate-source temporal` is the clean winners-first experiment: it mines breakout-derived seed
topics from a window before the holdout, freezes that list for the run, scores with pre-holdout
searches, and tests against the later holdout. Caveat: mining winners from an *older* pre-holdout
window is recall-limited — viewCount-ordered search increasingly surfaces incumbents as
small-at-publish videos age out of the top results, so the temporal path often finds few or no
pre-holdout seeds.
When that happens, lean on the forward test, which sidesteps the problem by snapshotting today's
winners and resolving them later. For the conservative curated baseline, use
`--candidate-source subtopics`.

Use `python -m youtube_niche.audit --out-dir out` when quota is exhausted or after a weak backtest.
It reads existing backtest Markdown reports, compares holdout breakout titles against curated and
discovered seeds, and flags whether the failure looks like seed-source coverage, matching, or
ranking/scoring. It does not call YouTube.

**No API key? Try it keyless.** `python -m youtube_niche.backtest --fixtures` runs the whole
backtest against built-in synthetic fixture data — no credentials, no quota. It's how CI and new
contributors exercise the scoring/validation logic before wiring up real auth.

### Forward testing

Use `--snapshot` on scoring runs, or capture any scored CSV with:

```bash
python -m youtube_niche.forward capture out/some-score.csv --label "AI shortlist"
python -m youtube_niche.forward summary   # how many checkpoints are pending / due
python -m youtube_niche.forward resolve   # check due checkpoints against real breakouts
```

This creates pending 30/60/90-day checkpoints in `out/forward-snapshots.csv`. The goal is to
compare today's scores against future small-at-publish breakouts instead of relying only on
retrospective proxy backtests.

`resolve` closes the loop with zero leakage: for every checkpoint whose due date has passed, it
mines small-at-publish breakout videos for that exact topic inside the prediction window
`[created_at, due_at]`, then marks the row `checked` with a `breakout_count` and a hit/miss note
(one search per topic, quota-guarded). Channels that were small when they published but later grew
past the cap still count as hits. Re-run it whenever `summary` shows rows are **due** — this is the
honest, prospective validation the backtest can only approximate.

### Community calibration — does the score actually work?

The cleanest proof isn't one person's runs — it's *many*. Pool resolved snapshots (yours plus any
contributed under [`community/`](community/)) into a score-vs-reality calibration curve:

```bash
python -m youtube_niche.community calibrate
```

The report's headline is **AUC**: the probability a niche that broke out was scored above one that
didn't (`0.50` = the score is noise, `1.00` = perfect), plus a hit-rate-by-score-band table. This
is the one number that says whether the tool earns its keep — and it gets more trustworthy with
every contributor. See [`community/README.md`](community/README.md) to add your data (≈5 minutes).

## Quota — the real constraint

Current YouTube Data API quota separates `search.list` from the general unit pool: projects have
a default **100 `search.list` calls/day** plus **10,000 units/day** for other endpoints.
`videos`/`channels`/`commentThreads` are 1 unit per call (batched 50/call). Invalid API requests
also cost at least one point/call, so the local counter charges failed API responses too.
The tool:

- **caches every response** to `.cache/youtube_niche.sqlite` — re-runs don't re-spend quota;
- **tracks search calls and unit usage separately** and refuses calls that would exceed budget;
- **batches** all videos/channels lookups, and stops cleanly when the budget runs low.

A topic costs ~`1 search + 2 + comment_videos×comment_pages` general units. With defaults, 20
seeds use about 20 search calls plus about 240 non-search units.

> **Search volume:** the YouTube API does *not* expose it. We approximate demand with median
> views/day, p75 views/day, successful recent uploads, Google Trends' YouTube Search direction,
> and comment requests. Trends is term-level interest, not per-video analytics. Autocomplete
> gives real query strings but not volume. Wire in vidIQ/Ahrefs later if you need hard
> search-volume numbers.
>
> **Google Trends:** the optional Trends signal uses `pytrends` with the YouTube Search property.
> It is cached, rate-limit prone, and best treated as a demand-prior signal rather than proof.

### External keyword/RPM data

If you have data from a keyword tool, ad platform, sponsor database, or your own research, pass it
as a CSV:

```bash
youtube-niche "ai tools for real estate" --metrics-csv data/keyword-metrics.csv
```

Accepted columns are intentionally flexible: `topic`, `keyword`, `query`, or `term` for the
phrase; `monthly_searches`, `search_volume`, `volume`, or `monthly_volume` for demand;
`cpm`, `rpm`, `estimated_cpm`, or `estimated_rpm` for monetization; and optional pre-normalized
`demand_score`, `cpm_score`, or `rpm_score` values.

## Tests

Logic and signals are tested offline (no keys/network):

```bash
python -m pytest -q        # or: python tests/test_logic.py
```

## Layout

```
youtube_niche/
  config.py          keys, quota budget, weights, thresholds
  cache.py           sqlite request cache (saves quota)
  youtube_client.py  quota-aware, cached YouTube Data API wrapper
  channel_size.py    subscribers-at-publish estimates and small-at-publish checks
  llm.py             pluggable LLM backends for signals E, G; no native X search
  transcript.py      transcript fetch (signal G)
  seeds.py           autocomplete seed expansion
  signals/           A outlier · B,C,D supply · E comments · F trends · G quality · relevance gate
  monetization.py    CPM/ad-intent proxy
  external.py        optional keyword-volume/RPM CSV import
  score.py           combine -> confidence-adjusted opportunity score
  report.py          CSV + Markdown
  topics.py          topic normalization, dedupe, and lightweight clustering
  cli.py             orchestration / entrypoint
  backtest.py        retrospective proxy validation against holdout breakouts
  benchmark.py       repeated temporal backtests + aggregate/weight-calibration loop
  weighting.py       validation-calibrated top-level weight suggestions
  forward.py         forward-test snapshot capture and summaries
  evidence.py        ranked video/channel proof rows
  evidence_snapshot.py persistent video/channel proof registry
tests/test_logic.py  offline tests
```

## Open Source Status

This is a public beta. It includes an MIT license, contribution guide, security policy, code of
conduct, CI, issue templates, an offline demo, backtest registry, and forward-test snapshots.

Before treating the scores as production-grade, the priority is to accumulate backtest/forward-test
evidence, add external keyword-volume/RPM imports, and publish anonymized real example outputs.
See [docs/ROADMAP.md](docs/ROADMAP.md), [docs/LAUNCH.md](docs/LAUNCH.md), and
[CHANGELOG.md](CHANGELOG.md).

## Tuning

Weights and thresholds live in `youtube_niche/config.py` (`Weights`, `Config`). Start with the
defaults, run a niche you know well, and adjust. Every component sub-score is in the CSV, which
makes it easy to see which signal is driving a rank.

For validation-led tuning, run several temporal benchmark windows first, then:

```bash
python -m youtube_niche.weighting --registry out/backtest-runs.csv
```

Only promote a new weight mix when it improves AUC/lift across enough non-circular windows to beat
the current opportunity score by more than noise.
