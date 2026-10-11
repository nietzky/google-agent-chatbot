"""Exercise the real ADK runner with scripted model output, without API charges."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from chatbot.agents import build_agents
from chatbot.memory import MemoryStore
from chatbot.products import Turn, current_turn


class ScriptedModel(BaseLlm):
    model: str = "scripted"
    replies: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(role="model", parts=[self.replies.pop(0)]))


async def test_orchestrator_delegates_and_memory_uses_real_user_context(tmp_path):
    memory = await MemoryStore.open(tmp_path / "memory.db")
    root, weather = build_agents(SimpleNamespace(chat_model="openai/gpt-4.1-mini"), None, memory)
    root.model = ScriptedModel(
        replies=[
            types.Part.from_function_call(
                name="memory_agent", args={"request": "I prefer Celsius"}
            ),
            types.Part(text="Remembered your preference."),
        ]
    )
    root.tools[2].agent.model = ScriptedModel(
        replies=[
            types.Part.from_function_call(
                name="save_memory",
                args={
                    "key": "temperature_unit",
                    "value": "celsius",
                    "kind": "preference",
                    "evidence": "I prefer Celsius",
                },
            ),
            types.Part(text="Saved temperature_unit."),
        ]
    )
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="test", user_id="alice", session_id="one")
    runner = Runner(agent=root, app_name="test", session_service=sessions)
    try:
        events = [
            event
            async for event in runner.run_async(
                user_id="alice",
                session_id="one",
                new_message=types.Content(role="user", parts=[types.Part(text="I prefer Celsius")]),
            )
        ]
        assert events[-1].content.parts[0].text == "Remembered your preference."
        assert (await memory.list("alice"))[0]["value"] == "celsius"
        assert await memory.list("bob") == []
    finally:
        await weather.close()
        await memory.close()


async def test_aws_turn_automatically_retrieves_audits_and_preserves_links(tmp_path):
    memory = await MemoryStore.open(tmp_path / "memory.db")
    knowledge = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {
                    "source": "aws/s3/test",
                    "chunk": 0,
                    "text": "Check permission policies.",
                    "product_id": "s3",
                    "score": 0.02,
                    "title": "S3 troubleshooting",
                    "url": "https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html",
                }
            ]
        )
    )
    turn_id = await memory.begin_interaction("alice", "one", "S3 AccessDenied", "s3")
    token = current_turn.set(Turn(turn_id, "alice", "one", "s3", "S3 AccessDenied"))
    root, connections = build_agents(
        SimpleNamespace(chat_model="openai/gpt-4.1-mini"), knowledge, memory
    )
    root.model = ScriptedModel(replies=[types.Part(text="Check your permissions.")])
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="test", user_id="alice", session_id="one")
    try:
        runner = Runner(agent=root, app_name="test", session_service=sessions)
        events = [
            event
            async for event in runner.run_async(
                user_id="alice",
                session_id="one",
                new_message=types.Content(role="user", parts=[types.Part(text="S3 AccessDenied")]),
            )
        ]
        final = "".join(p.text or "" for p in events[-1].content.parts)
        assert "Related AWS documentation" in final and "troubleshoot-403-errors.html" in final
        knowledge.search.assert_awaited_once_with("S3 AccessDenied", "hybrid", product_id="s3")
        assert any(
            r["relationship"] == "retrieved"
            for r in (await memory.history("alice"))["interactions"]
        )
    finally:
        current_turn.reset(token)
        await connections.close()
        await memory.close()
