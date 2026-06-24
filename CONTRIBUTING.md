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

## Good First Contributions

- Run `youtube-niche --demo` and tell us whether the report is understandable.
- Run a small live scan on a niche you know and open an Evaluation feedback issue.
- Better relevance matching tests.
- More domain/subtopic calibration data.
- Backtest improvements and aggregate metrics.
- External keyword-volume or RPM importers.
- Documentation examples using anonymized or synthetic data.

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
