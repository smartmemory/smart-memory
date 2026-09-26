# Rules-card golden replay

`session.json` is the neutral input. Response provenance:

- `response.json`: **SYNTHETIC**, manually authored model-shaped edge case. The
  prefixed observation intentionally contains no legacy regex keywords, to guard
  prefix-only promotion. This is not evidence of a live provider run.
- `response-live-tagged.json`: live Groq recording via the wrapper's lesson path,
  2026-09-26, `session.json` input (controller run 4); both partner rules tagged.
- `response-live-untagged.json`: live Groq recording via the wrapper's lesson path,
  2026-09-26, `session.json` input (controller run 2); no constraints tagged.

Tests replay raw JSON through the real reasoning and decision extractors and
capture journal, never mock extracted conclusions.
