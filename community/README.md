# Community validation — pool the ground truth

This tool predicts which niches a newcomer can win. The honest question is: **do high scores
actually predict breakouts?** No single user can answer that quickly — a forward test takes
30–90 days, and backtests burn API quota. But *together* we can, and every run you do helps.

This directory collects **resolved forward-test snapshots** from contributors. Pool enough of
them and we get a public **score-vs-reality calibration curve** — the real, leakage-free measure
of whether the tool works.

## How to contribute data (≈5 minutes of your time, spread over weeks)

1. **Score and snapshot** any run:
   ```bash
   python -m youtube_niche.winners --domain "personal finance" --snapshot
   # or: python -m youtube_niche "<your niche>" --snapshot
   ```
   This appends pending 30/60/90-day checkpoints to `out/forward-snapshots.csv`.

2. **Wait, then resolve** (re-run whenever `forward summary` shows rows are *due*):
   ```bash
   python -m youtube_niche.forward summary    # how many are due?
   python -m youtube_niche.forward resolve     # checks due rows against real breakouts
   ```
   `resolve` marks each due row `checked` with a `breakout_count` and a hit/miss note. This is
   **prospective and leakage-free** — the prediction was recorded before the outcome existed.

3. **Validate and submit.** Copy your resolved CSV here with a descriptive name, then:
   ```bash
   python -m youtube_niche.community validate community/your-handle-finance-2026.csv
   ```
   If it prints `OK`, open a PR adding the file under `community/` (top level).

## Build the calibration curve

```bash
python -m youtube_niche.community calibrate          # pools out/forward-snapshots.csv + community/*.csv
```

This writes `out/calibration-*.md` with the headline **AUC** (probability a niche that broke out
scored above one that didn't: 0.50 = noise, 1.00 = perfect), a hit-rate-by-score-band table, and
top-half vs bottom-half lift. See `examples/sample-resolved-snapshots.csv` for the row format.

## Schema (a subset of the forward-snapshot columns)

Required for a valid submission: **`topic`, `opportunity`, `status`, `breakout_count`**. Keep the
other forward-snapshot columns too — they make the data richer. Rules:

| Column | Meaning |
|---|---|
| `topic` | the scored niche |
| `opportunity` | the 0–1 opportunity score at snapshot time |
| `status` | must be `checked` (resolved) to count; `pending` rows are ignored |
| `breakout_count` | integer ≥ 0 — small-channel breakouts found in the prediction window |
| `created_at` / `due_at` / `checked_at` | when scored / due / resolved |

## Ground rules

- **Public data only.** These are public YouTube metrics and your own scores — no API keys,
  tokens, emails, or personal data. The validator does not check for secrets; you are responsible.
- **Don't hand-edit outcomes.** The value of this dataset is that `resolve` produced the
  `breakout_count`, not a human. Submit what the tool measured.
- Files under `examples/` are illustrative and are **excluded** from the pooled calibration.
