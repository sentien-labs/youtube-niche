# Examples

Start with the synthetic offline demo:

```bash
youtube-niche --demo
```

or from a source checkout:

```bash
python -m youtube_niche --demo
```

The command writes a CSV and Markdown report to `out/` without YouTube credentials, Google
Trends access, or an LLM backend. The numbers are synthetic, but the report shape and caveats
match the live scorer.

When this repository is public, real example reports should be anonymized before committing:

- remove private research labels and client/customer names;
- avoid raw cache databases;
- include the command and major flags used;
- mention whether Trends, comments, and LLM quality scoring were enabled.
