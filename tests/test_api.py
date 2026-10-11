import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from chatbot.api import ChatService, create_app
from chatbot.runtime import APP


class TestModel(BaseLlm):
    __test__ = False
    model: str = "test"
    delay: float = 0
    fail: bool = False

    async def generate_content_async(self, llm_request, stream=False):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_DETAIL")
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="A test answer.")])
        )


def make_service(model=None):
    memory = SimpleNamespace(
        learning=None,
        list=AsyncMock(return_value=[]),
        forget=AsyncMock(return_value={"deleted": 1}),
        begin_interaction=AsyncMock(return_value="turn-1"),
        finish_interaction=AsyncMock(),
        delete_session_history=AsyncMock(),
        history=AsyncMock(return_value={"interactions": [], "incidents": []}),
    )
    knowledge = SimpleNamespace(
        index="test",
        es=SimpleNamespace(
            search=AsyncMock(
                return_value={
                    "aggregations": {"sources": {"buckets": [{"key": "handbook", "doc_count": 1}]}}
                }
            )
        ),
    )
    toolset = SimpleNamespace(close=AsyncMock())

    def factory(*args):
        return Agent(name="orchestrator", model=model or TestModel()), toolset

    service = ChatService(
        SimpleNamespace(web_user_id="web-user"),
        memory,
        knowledge,
        InMemorySessionService(),
        agent_factory=factory,
    )
    return service, toolset


def test_conversation_stream_history_and_delete():
    service, toolset = make_service()
    with TestClient(create_app(service)) as client:
        created = client.post("/api/conversations", json={"title": "My question"})
        assert created.status_code == 201
        identifier = created.json()["id"]
        response = client.post(f"/api/conversations/{identifier}/messages", json={"text": "Hello"})
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "event: message" in response.text and "A test answer." in response.text
        assert "event: done" in response.text
        history = client.get(f"/api/conversations/{identifier}/messages").json()
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert [item["text"] for item in history] == ["Hello", "A test answer."]
        assert client.get("/api/conversations").json()[0]["title"] == "My question"
        assert not service.active
        service.memory.finish_interaction.assert_awaited_once_with("turn-1", "completed")
        toolset.close.assert_awaited_once()
        assert client.delete(f"/api/conversations/{identifier}").status_code == 204
        assert client.get(f"/api/conversations/{identifier}/messages").status_code == 404


def test_validation_origin_identity_and_busy_session():
    service, _ = make_service()
    private_id = str(uuid.uuid4())
    asyncio.run(
        service.sessions.create_session(app_name=APP, user_id="someone-else", session_id=private_id)
    )
    with TestClient(create_app(service)) as client:
        assert client.get(f"/api/conversations/{private_id}/messages").status_code == 404
        assert client.get("/api/conversations").json() == []
        assert client.post("/api/conversations", json={"title": "   "}).status_code == 422
        assert (
            client.post("/api/conversations", json={"user_id": "someone-else"}).status_code == 422
        )
        assert (
            client.post(
                "/api/conversations", json={}, headers={"origin": "https://example.com"}
            ).status_code
            == 403
        )
        identifier = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{identifier}/messages"
        assert client.post(endpoint, json={"text": " "}).status_code == 422
        assert client.post(endpoint, json={"text": "x" * 12001}).status_code == 422
        assert (
            client.post(endpoint, json={"text": "help", "product_id": "not-real"}).status_code
            == 422
        )
        service.reserve(identifier)
        assert client.post(endpoint, json={"text": "hello"}).status_code == 409
        assert client.delete(f"/api/conversations/{identifier}").status_code == 409
        service.active.clear()


def test_memory_library_and_sanitized_provider_errors():
    service, _ = make_service(TestModel(fail=True))
    with TestClient(create_app(service)) as client:
        assert client.get("/api/library").json()["chunks"] == 1
        assert client.get("/api/memories").json() == []
        assert client.request("DELETE", "/api/memories", json={"key": "units"}).json() == {
            "deleted": 1
        }
        service.memory.forget.assert_awaited_once_with("web-user", "units")
        identifier = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{identifier}/messages", json={"text": "hello"})
        assert "event: error" in response.text
        assert "PRIVATE_PROVIDER_DETAIL" not in response.text
        assert not service.active
        service.knowledge.es.search.side_effect = RuntimeError("ES credentials must not leak")
        assert client.get("/api/library").json() == {"available": False, "sources": [], "chunks": 0}


async def test_disconnect_cancels_model_and_releases_session():
    service, toolset = make_service(TestModel(delay=60))
    identifier = str(uuid.uuid4())
    await service.sessions.create_session(app_name=APP, user_id=service.user, session_id=identifier)
    service.reserve(identifier)
    stream = service.stream(identifier, "hello")
    first = await anext(stream)
    assert "event: status" in first
    await stream.aclose()
    assert not service.active
    toolset.close.assert_awaited_once()
    service.memory.finish_interaction.assert_awaited_once_with("turn-1", "cancelled")


def test_global_capacity_limit():
    service, _ = make_service()
    for i in range(3):
        service.reserve(str(i))
    with pytest.raises(Exception) as exc:
        service.reserve("fourth")
    assert exc.value.status_code == 429
