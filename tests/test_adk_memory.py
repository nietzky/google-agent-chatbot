"""ADK persistence, ownership and stale-index protection without model/network calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from chatbot.adk_memory import PingoMemoryService
from chatbot.memory import MemoryStore
from chatbot.products import Turn, current_turn


def fact(key, value):
    return MemoryEntry(
        id=key,
        content=types.Content(parts=[types.Part(text=value)]),
        custom_metadata={"kind": "takeaway", "evidence": value},
    )


async def test_adk_persistence_scope_correction_and_forgetting(tmp_path):
    path = tmp_path / "memory.db"
    store = await MemoryStore.open(path)
    service = PingoMemoryService(store, "pingo")
    await service.add_memory(
        app_name="pingo", user_id="alice", memories=[fact("runtime", "Python 3.13")]
    )
    await store.close()
    store = await MemoryStore.open(path)
    service = PingoMemoryService(store, "pingo")
    try:
        recalled = await service.search_memory(app_name="pingo", user_id="alice", query="runtime")
        assert recalled.memories[0].custom_metadata["value"] == "Python 3.13"
        assert recalled.memories[0].timestamp
        assert not (
            await service.search_memory(app_name="pingo", user_id="bob", query="runtime")
        ).memories
        with pytest.raises(ValueError):
            await service.search_memory(app_name="other", user_id="alice", query="runtime")
        await service.add_memory(
            app_name="pingo", user_id="alice", memories=[fact("runtime", "Python 3.14")]
        )
        assert len(await store.list("alice")) == 1
        await store.forget("alice", "runtime")
        assert not (
            await service.search_memory(app_name="pingo", user_id="alice", query="Python")
        ).memories
    finally:
        await store.close()


async def test_stale_search_never_resurrects_deleted_corrected_or_expired_facts(tmp_path):
    store = await MemoryStore.open(tmp_path / "memory.db")
    es = SimpleNamespace(search=AsyncMock())
    service = PingoMemoryService(store, "pingo", es)
    try:
        await store.save("alice", "runtime", "Python 3.12", "takeaway", "Python 3.12")
        old = (await store.list("alice"))[0]
        es.search.return_value = {"hits": {"hits": [{"_source": old}]}}
        await store.save("alice", "runtime", "Python 3.13", "takeaway", "Python 3.13")
        response = await service.search_memory(app_name="pingo", user_id="alice", query="Python")
        assert response.memories[0].custom_metadata["value"] == "Python 3.13"
        await store.db.execute("UPDATE memories SET expires_at='2000-01-01T00:00:00Z'")
        assert not (
            await service.search_memory(app_name="pingo", user_id="alice", query="Python")
        ).memories
        await store.forget("alice", "runtime")
        assert not (
            await service.search_memory(app_name="pingo", user_id="alice", query="Python")
        ).memories
        filters = es.search.call_args.kwargs["query"]["bool"]["filter"]
        assert {"term": {"user_id": "alice"}} in filters
        assert {"term": {"app_name": "pingo"}} in filters
    finally:
        await store.close()


async def test_outbox_retries_and_removes_projection(tmp_path):
    store = await MemoryStore.open(tmp_path / "memory.db")
    es = SimpleNamespace(index=AsyncMock(), delete=AsyncMock())
    es.options = lambda **kwargs: es
    service = PingoMemoryService(store, "pingo", es)
    try:
        await store.save("alice", "runtime", "Python", "takeaway", "Python")
        es.index.side_effect = RuntimeError("offline")
        with pytest.raises(RuntimeError):
            await service.sync_one()
        es.index.side_effect = None
        assert await service.sync_one()
        assert es.index.call_args.kwargs["document"]["app_name"] == "pingo"
        assert not await service.sync_one()
        await store.forget("alice", "runtime")
        assert await service.sync_one()
        es.delete.assert_awaited_once()
    finally:
        await store.close()


async def test_session_ingestion_idempotent_and_no_raw_transcripts(tmp_path):
    store = await MemoryStore.open(tmp_path / "memory.db")
    service = PingoMemoryService(store, "pingo")
    try:
        await store.db.execute("INSERT INTO memory_learning_state(id,enabled) VALUES(1,1)")
        turn = await store.begin_interaction("alice", "one", "My runtime is Python")
        await store.finish_interaction(turn, "completed")
        session = SimpleNamespace(app_name="pingo", user_id="alice", id="one")
        await service.add_session_to_memory(session)
        await service.add_session_to_memory(session)
        assert (await (await store.db.execute("SELECT count(*) FROM memory_jobs")).fetchone())[
            0
        ] == 1
        assert not await store.list("alice")
    finally:
        await store.close()


async def test_product_filter_and_search_outage_fallback(tmp_path):
    store = await MemoryStore.open(tmp_path / "memory.db")
    service = PingoMemoryService(
        store, "pingo", SimpleNamespace(search=AsyncMock(side_effect=RuntimeError()))
    )
    token = current_turn.set(Turn("turn", "alice", "session", "s3"))
    try:
        for key in ("s3", "ec2", "style"):
            await store.save("alice", key, key, "takeaway", key)
        await store.db.execute("UPDATE memories SET product_id=key WHERE key IN ('s3','ec2')")
        result = await service.search_memory(app_name="pingo", user_id="alice", query="ec2")
        assert {entry.id for entry in result.memories} == {"s3", "style"}
    finally:
        current_turn.reset(token)
        await store.close()


async def test_real_adk_load_memory_tool_uses_service(tmp_path):
    from google.adk.agents import Agent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools.load_memory_tool import load_memory

    class RecallModel(BaseLlm):
        model: str = "scripted"
        calls: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            self.calls += 1
            part = (
                types.Part.from_function_call(name="load_memory", args={"query": "runtime"})
                if self.calls == 1
                else types.Part(text="Recalled.")
            )
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    store = await MemoryStore.open(tmp_path / "runner.db")
    service = PingoMemoryService(store, "pingo")
    try:
        await service.add_memory(
            app_name="pingo", user_id="alice", memories=[fact("runtime", "Python 3.13")]
        )
        sessions = InMemorySessionService()
        await sessions.create_session(
            app_name="pingo", user_id="alice", session_id="new-conversation"
        )
        runner = Runner(
            app_name="pingo",
            agent=Agent(name="recall", model=RecallModel(), tools=[load_memory]),
            session_service=sessions,
            memory_service=service,
        )
        events = [
            event
            async for event in runner.run_async(
                user_id="alice",
                session_id="new-conversation",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="What runtime do I use?")]
                ),
            )
        ]
        responses = [response for event in events for response in event.get_function_responses()]
        assert "Python 3.13" in responses[0].model_dump_json()
    finally:
        await store.close()
