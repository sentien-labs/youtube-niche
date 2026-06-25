# Contributing

Thanks for helping improve YouTube Niche Finder.

## What This Project Optimizes For

The project is trying to identify YouTube niches with:

- real demand;
- thin or stale supply;
- evidence that small/newer channels can win;
- transparent confidence and caveats.

Please keep changes explainable. A weaker score with clear evidence is better than a flashy score
that hides missing or noisy data.

## Local Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

Copy `.env.example` to `.env` only if you want to run live YouTube/Trends/LLM signals.

## No API key? You can still contribute

The whole pipeline runs keyless on built-in fixtures, so you can improve scoring/validation logic
with zero credentials:

```bash
python -m youtube_niche --demo                 # synthetic scored report
python -m youtube_niche.backtest --fixtures    # full backtest on fixture data
python -m youtube_niche.community calibrate --community-dir community/examples \
    --snapshot-path /dev/null                  # build a calibration curve from the sample
```

## Contribute validation data (the highest-leverage thing you can do)

The tool's scarcest resource is **ground truth**: did a high score actually predict a breakout?
You can supply it. Score a run with `--snapshot`, wait, run `forward resolve`, then submit the
resolved CSV under `community/`. Full walkthrough in [`community/README.md`](community/README.md).
Pooled submissions become a public score-vs-reality calibration curve (AUC) — the number that
proves whether the tool works.

## Good First Contributions

Each of these maps to one pluggable seam, so they're easy to do in isolation (add a test that
follows the fake-client pattern in `tests/test_logic.py`):

- **Add a domain** to `domains.py` *and run `python -m youtube_niche.calibrate` to set its knee* —
  paste the calibration output in your PR.
- **Add an LLM backend** in `llm.py` (follow `CodexCliBackend` / `AgyCliBackend`).
- **Add an external-metrics adapter** in `external.py` (vidIQ / TubeBuddy / Ahrefs CSV export).
- **Add or sharpen a signal** under `signals/` — keep it explainable and tested.
- Run `youtube-niche --demo` (or `--fixtures` backtest) and tell us whether the report is clear.
- Run a small live scan on a niche you know and open an Evaluation feedback issue.

## Evaluation Feedback

Community evaluation is the fastest way to make the score useful. If you know a niche well,
run a small scan and tell us where the tool is right or wrong.

Useful feedback includes:

- the exact command you ran;
- the top 5 topics and confidence scores;
- which topics are genuinely underserved;
- which topics are saturated, irrelevant, too broad, or too low-demand;
- public YouTube examples that support your judgment;
- which signal appears to need work, if you can tell.

Please use the Evaluation feedback issue template. Anonymized observations are welcome; private
cache databases, API keys, client names, and sensitive reports are not.

## Pull Request Checklist

- Add or update offline tests for scoring logic.
- Keep generated `out/`, `.cache/`, `.env`, and virtualenv files out of commits.
- Document quota impact when adding API calls.
- Be explicit about known limitations and confidence effects.

## Data And API Safety

Do not commit API keys, OAuth tokens, raw private reports, or cache databases. If a contribution
needs sample data, use small synthetic fixtures or anonymized excerpts.
