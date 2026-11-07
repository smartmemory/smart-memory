# Session lesson extraction, SM-HOOKS-2-0-1

Contract: [session-lesson-contract.json](session-lesson-contract.json).

The production session extractor and read-only measurement use the same parser.
Each explicit standing rule produces one conclusion with content
`CONSTRAINT: User-stated rule: ` followed by the exact user text. Preserve
punctuation, negation, exceptions, units, identifier category and scope. A bare
assistant acknowledgment is not evidence of implementation and adds no lesson.
Source-verified rule-only batches use the constraint category without a redundant
classifier call. Mixed batches still classify independent findings.
Distinct evidence-backed findings and learned external constraints remain separate.

Parse the entire JSON response, validate the schema before constructing a trace,
and do not truncate rules. One deterministic post-step handles an unambiguous
conversation containing exactly one single-line standing rule and only bare
acknowledgments: replace redundant/paraphrased model rows with that source rule.
This repair requires an unambiguous standing directive, prohibition or scoped
term definition. Definitions such as "In this project, X is/means Y, not Z",
"For this project, X refers to Y" and "In this project, treat X as Y" fix a term's
meaning and are standing policies. They share canonicalization, classifier bypass
and assistant acknowledgment collapse with directives and prohibitions. A scope
phrase alone, a status report, a past event or a one-off task never qualifies.
Otherwise model output, including `[]`, stands and classification remains active.
The producer carries trusted role/content records alongside its JSON serialized
prompt. Text containing role headers cannot introduce a user turn. Attribution
must equal a complete user sentence, allowing whitespace differences only.
The [processing contract](session-lesson-processing-contract.json) specifies this
boundary and the receipt diagnostics. Repairs, duplicates removed and findings
dropped are counted in `lesson_output_diagnostics` and retain WARNING logs.
Drops make `lessons_complete` false, including after plan replay.
For other conversations deduplicate exact content only. Validate every entry
before applying the existing eight-lesson bound. Keep attributed rules ahead of
excess findings and warn about discarded findings. Refuse more than eight
explicit rules instead of silently dropping a rule. Attributed rule text must
match a user source verbatim. Refuse invalid JSON, invalid schema, invented or
ambiguous attribution with WARNING and no additional model call. Do not guess
at semantic similarity or repair missing negation from model text alone.

The 20-row historical replay tests parsing and post-processing only. It cannot
prove new prompt quality. Historical sample 14 contains only an error message,
so replay must refuse it. Live quality still requires the unchanged 20/20 gate.
