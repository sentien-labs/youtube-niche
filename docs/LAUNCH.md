# Launch Notes

This project should launch as a public beta, with the ask centered on evaluation rather than hype.

## Positioning

YouTube Niche Finder is an open-source evidence engine for finding potentially underserved
YouTube niches. It combines public YouTube data, Google Trends, optional LLM signals, optional
external keyword/RPM data, and validation harnesses.

It is not a promise that a topic will go viral. The goal is to make niche research more
explainable and easier to improve with community feedback.

## Short Launch Copy

I built an open-source YouTube niche research CLI.

It scores topics on demand, low supply, small-channel beatability, monetization, Trends, comments,
content depth, confidence, and multi-query consensus. It also includes an offline demo plus
backtest and forward-test harnesses.

I’m looking for creators, SEO people, and data-minded builders to test it on niches they know well
and tell me where the scoring is wrong.

Repo: https://github.com/vswarm-ai/youtube-niche

Try without keys:

```bash
git clone https://github.com/vswarm-ai/youtube-niche.git
cd youtube-niche
python3 -m venv venv
source venv/bin/activate
pip install -e .
youtube-niche --demo
```

## Show HN Draft

Title:

```text
Show HN: Open-source YouTube niche finder with explainable scoring and backtests
```

Body:

```text
I built YouTube Niche Finder, an open-source CLI for finding potentially underserved YouTube niches.

It scores topics using public YouTube data, small-channel beatability, demand velocity, low-supply
signals, monetization proxies, Google Trends, optional comment/LLM signals, confidence, and
multi-query consensus.

The main thing I want from this launch is evaluation. If you know a niche well, run it and tell me
where the scoring is wrong. The repo includes an offline demo, issue template for evaluation
feedback, backtest harness, and forward-test snapshots.

Repo: https://github.com/vswarm-ai/youtube-niche
```

## Feedback Request

The most useful feedback answers one of these:

- Which top-ranked niches are genuinely underserved?
- Which are saturated, irrelevant, too broad, or too low-demand?
- Which signal seems wrong?
- What public YouTube examples prove it?
- What would make the report easier to trust?

Use the Evaluation feedback issue template:

https://github.com/vswarm-ai/youtube-niche/issues/new?template=evaluation_feedback.md
