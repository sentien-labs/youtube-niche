# YouTube Niche Finder

[![CI](https://github.com/vswarm-ai/youtube-niche/actions/workflows/ci.yml/badge.svg)](https://github.com/vswarm-ai/youtube-niche/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Find **high-demand, high-monetization, low-supply** YouTube topics. Give it a niche; it expands
to candidate topics, pulls data from the YouTube Data API, scores each on demand, monetization,
supply, content depth, confidence, and query consensus, then writes an explainable CSV + Markdown
report.

The guiding principle: a real opportunity needs demand, monetization, and thin supply at the
same time. High demand + high supply = saturated. Low demand + low supply = usually nobody
cares. High CPM + no demand = not enough. The score gates low view velocity and shows confidence
so missing evidence cannot quietly look like certainty.

## Quick Evaluation

The easiest way to help is to run the tool on a niche you already understand and tell us where
the ranking feels right or wrong.

### 1. Try The Offline Demo

No API keys needed:

```bash
git clone https://github.com/vswarm-ai/youtube-niche.git
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
| A | **Outlier** | videos + channels | views ÷ subscribers — topic-carried hits = beatability/portability context |
| B | **Supply age** | search `publishedAt` | stale top results = abandoned demand |
| C | **Competition** | search results | few credible videos + low authority concentration = thin supply |
| D | **Small channels** | channels | small channels ranking = beatable |
| E | **Comment demand** | commentThreads + LLM | "please make a video on X" — literal unmet demand |
| F | **Trends** | Google Trends (YouTube Search source) | rising interest + breakout queries |
| G | **Content depth** | transcripts + LLM | how thin the top-ranking videos actually are |
| H | **Monetization** | curated CPM + keyword intent | domain CPM and advertiser intent proxy |
| H2 | **External metrics** | optional CSV | imported search-volume/RPM evidence |
| I | **Relevance gate** | title match | caps demand when search results do not clearly match the niche |
| J | **Confidence** | signal coverage | how complete the evidence is |

`opportunity = confidence × demand_gate × geomean(demand, low-supply, monetization, thin-content)`.
The demand gate includes both view velocity and a relevance gate, so unrelated fuzzy search
results cannot create a fake "high demand / low supply" opportunity.
The raw opportunity and confidence-adjusted opportunity are both shown. Optional missing signals
lower confidence instead of being silently treated as strong evidence.

Use `--query-samples 3` for more robust scoring. The scorer evaluates several query phrasings,
uses median signal values across relevant samples, and lowers confidence when query coverage or
agreement is weak.

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
# set, else the codex CLI. Or pick: codex | claude | agy (CLIs use their own auth) | anthropic.
LLM_PROVIDER=codex
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
| `anthropic` | SDK | needs `ANTHROPIC_API_KEY` |

Pick with `--llm-provider` or `LLM_PROVIDER`. `auto` = anthropic key if present, else codex.
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

# Backtest — check whether high-ranked candidates match later small-channel breakouts
python -m youtube_niche.backtest --domain "AI" --query-samples 2 --max-candidates 10
python -m youtube_niche.backtest --aggregate

# Forward-test — save today's scored topics for 30/60/90-day follow-up
python -m youtube_niche "off-grid solar for vans" --snapshot
python -m youtube_niche.forward summary
```

The domain list and its (industry-estimate) CPM ranges live in `youtube_niche/domains.py` —
edit freely. CPM is *not* available from the YouTube API; the registry is the curated input.

Useful flags:

| Flag | Effect |
|------|--------|
| `--max-seeds N` | cap candidate topics (default 20) |
| `--top-n N` | search results scanned per seed (default 30) |
| `--query-samples N` | search-query variants per topic; use 3 to reduce single-search noise |
| `--alphabet-soup` | aggressive autocomplete expansion (more seeds) |
| `--no-llm` | skip comment + depth signals (no Anthropic key needed) |
| `--no-trends` | skip Google Trends (faster, avoids rate-limits) |
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

1. mine small-channel breakout videos in a holdout window;
2. score candidate niches using searches restricted to videos published before that holdout;
3. report whether high-ranked candidates matched the later breakout videos.

This is directional, not a perfect historical replay. The YouTube Data API does not provide
historical view/subscriber snapshots, so pre-holdout videos still carry current public counts.
By default the harness disables comments, LLM quality, and Trends to reduce future leakage; add
`--with-comments`, `--with-llm`, or `--with-trends` when you intentionally want those signals.
Backtest runs append to `out/backtest-runs.csv`; use `python -m youtube_niche.backtest --aggregate`
to generate a cross-run validation summary.

### Forward testing

Use `--snapshot` on scoring runs, or capture any scored CSV with:

```bash
python -m youtube_niche.forward capture out/some-score.csv --label "AI shortlist"
python -m youtube_niche.forward summary
```

This creates pending 30/60/90-day checkpoints in `out/forward-snapshots.csv`. The goal is to
compare today's scores against future small-channel breakouts instead of relying only on
retrospective proxy backtests.

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
  llm.py             Anthropic wrapper (signals E, G); degrades if no key
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
  forward.py         forward-test snapshot capture and summaries
tests/test_logic.py  offline tests
```

## Open Source Status

The repo is being prepared for a public release. It already includes an MIT license, contribution
guide, security policy, code of conduct, CI, issue templates, an offline demo, backtest registry,
and forward-test snapshots.

Before treating the scores as production-grade, the priority is to accumulate backtest/forward-test
evidence, add external keyword-volume/RPM imports, and publish anonymized real example outputs.
See [docs/ROADMAP.md](docs/ROADMAP.md).

## Tuning

Weights and thresholds live in `youtube_niche/config.py` (`Weights`, `Config`). Start with the
defaults, run a niche you know well, and adjust. Every component sub-score is in the CSV, which
makes it easy to see which signal is driving a rank.
