"""CLAIMS-02-FIX1: the Lite daemon's synthesized ``POST /memory/ask`` goes through core's one grounding path.

Before this, the daemon built its own prompt and called the model itself: zero evidence still reached the model,
nothing checked the citations, and the response had no ``used_evidence_ids``/``grounded``. Real disposable Lite
storage, the model replaced by a local fake, provider keys neutered (the one key set is a bogus Groq key the fake
model caller never sends anywhere).
"""

import threading

import pytest
from fastapi.testclient import TestClient

from smartmemory_app import local_api, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS

NO_RECORD_ANSWER = "No stored record matches this question."
UNTIED_ANSWER = "The answer could not be tied to a stored record."
ITEM_ID = "3f2a9c1b7d4e40a8b6c5d2e1f0a9b8c7"


@pytest.fixture()
def synthesized_ask(tmp_path, monkeypatch):
    from smartmemory.tools.factory import create_lite_memory

    for key in (*LLM_KEY_ENV_VARS, "GEMINI_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "bogus-not-a-real-key")
    monkeypatch.setattr(local_api, "llm_key_present", lambda: True)
    memory = create_lite_memory(str(tmp_path / "claims02_store"), spawn_worker=False)
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    monkeypatch.setattr(local_api, "get_memory", lambda: memory)
    calls: list[dict] = []
    replies: list[str] = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return "model", replies.pop(0) if replies else "ANSWER: unset"

    monkeypatch.setattr("smartmemory.utils.llm.call_llm", fake_llm)
    with TestClient(local_api.api) as client:
        try:
            yield memory, client, calls, replies
        finally:
            memory.close()


def _store_consent_record(memory):
    from smartmemory.models.memory_item import MemoryItem

    memory.add(
        MemoryItem(
            item_id=ITEM_ID,
            content="Site 03 used informed consent form version 3.0.",
            memory_type="episodic",
        )
    )


def test_zero_evidence_answers_no_record_without_a_model_call(synthesized_ask):
    _memory, client, calls, _replies = synthesized_ask

    response = client.post("/ask", json={"question": "What is the capital of Mongolia?"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == []
    assert body["answer"] == NO_RECORD_ANSWER
    assert body["grounded"] is False
    assert body["used_evidence_ids"] == []
    assert body["evidence"] == []


def test_a_cited_answer_returns_the_cited_id_with_type_and_date(synthesized_ask):
    memory, client, calls, replies = synthesized_ask
    _store_consent_record(memory)
    # Upper-cased and unbracketed: still a citation of the one record shown (CLAIMS-02-FIX1).
    replies.append(f"ANSWER: Version 3.0, per {ITEM_ID.upper()}.\nREASONING: The site 03 record.")

    response = client.post("/ask", json={"question": "Which consent form did site 03 use?"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(calls) == 1
    assert "square brackets" in calls[0]["user_content"]
    assert f"[{ITEM_ID}]" in calls[0]["user_content"]
    assert body["grounded"] is True
    assert body["used_evidence_ids"] == [ITEM_ID]
    assert body["answer"] == f"Version 3.0, per {ITEM_ID.upper()}."
    evidence = {item["item_id"]: item for item in body["evidence"]}
    assert evidence[ITEM_ID]["memory_type"] == "episodic"
    assert evidence[ITEM_ID]["created_at"]


def test_an_uncited_answer_is_replaced_by_the_untied_text_beside_its_evidence(synthesized_ask):
    memory, client, calls, replies = synthesized_ask
    _store_consent_record(memory)
    replies.append("ANSWER: Version 9.9 from memory.\nREASONING: I recall it.")

    response = client.post("/ask", json={"question": "Which consent form did site 03 use?"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(calls) == 1
    assert body["answer"] == UNTIED_ANSWER
    assert body["grounded"] is False
    assert body["used_evidence_ids"] == []
    assert body["evidence"]
    assert body["reasoning"] == "I recall it."


def test_the_store_lock_is_released_while_the_model_answers(synthesized_ask, monkeypatch):
    """A model call reads no store and can take seconds: other daemon requests do not wait on it."""
    memory, client, _calls, _replies = synthesized_ask
    _store_consent_record(memory)
    seen = []

    def probing_llm(**_kwargs):
        def other_request():
            got = local_api._rw_lock.acquire(timeout=2)
            seen.append(got)
            if got:
                local_api._rw_lock.release()

        worker = threading.Thread(target=other_request)
        worker.start()
        worker.join()
        return "model", f"ANSWER: Version 3.0 [{ITEM_ID}]."

    monkeypatch.setattr("smartmemory.utils.llm.call_llm", probing_llm)

    response = client.post("/ask", json={"question": "Which consent form did site 03 use?"})

    assert response.status_code == 200, response.text
    assert seen == [True]
    assert response.json()["grounded"] is True
