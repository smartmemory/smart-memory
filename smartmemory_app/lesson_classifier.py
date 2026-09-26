"""Dedicated external-constraint classifier shared by capture and migration."""

import json
import os
import re

from smartmemory.utils.llm import call_llm, get_last_usage

MODEL = "openai/gpt-oss-120b"
PROMPT = """Classify each engineering lesson. Answer EXTERNAL only when the rule is imposed from OUTSIDE this codebase and
the team cannot change it by editing code: a third-party API, bank, provider or platform behaviour (a rejection, an
error code, a required format or limit), a regulation, or a policy an owner, customer or partner stated.
Answer INTERNAL for anything this codebase's own authors decided or implemented: validation the code performs,
exceptions it raises, defaults, data structures, test facts, refactors, and what a component returns.
Examples: "Acme Pay rejects batches above 50 entries with AP-409" -> EXTERNAL. "The owner requires all exports to
be UTF-8" -> EXTERNAL. "parse_amount raises ValueError for negative input" -> INTERNAL. "Queue.snapshot returns
detached copies" -> INTERNAL.
Return a JSON object {"external": [indices]} and nothing else.

"""


def classify_lessons(texts, receipt):
    """Validate the whole response before returning kinds; preserve call usage."""
    from smartmemory_app.session_lessons import _lesson_usage

    if not texts:
        return []
    get_last_usage()
    try:
        if os.environ.get("SMARTMEMORY_CAPTURE_OFFLINE") == "1":
            raise RuntimeError("offline capture disables lesson classification")
        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError("lesson classification requires GROQ_API_KEY")
        _, response = call_llm(
            model=MODEL,
            api_key=os.environ["GROQ_API_KEY"],
            api_base="https://api.groq.com/openai/v1",
            temperature=0,
            max_output_tokens=8000,
            user_content=PROMPT
            + "\n".join(f"{i}. {text}" for i, text in enumerate(texts)),
        )
        match = re.search(r"\{.*\}", response or "", re.S)
        if match is None:
            raise ValueError("classifier returned no JSON object")
        result = json.loads(match.group(0))
        external = result.get("external")
        if not isinstance(external, list) or any(
            type(index) is not int or not 0 <= index < len(texts) for index in external
        ):
            raise ValueError(
                "classifier external indices missing, invalid or out of range"
            )
        return ["constraint" if i in external else "finding" for i in range(len(texts))]
    finally:
        receipt["lesson_classifier_usage"] = _lesson_usage(get_last_usage(), MODEL)
