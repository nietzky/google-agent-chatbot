"""Relational evidence distinguishes user activity from retrieved candidate products."""

import pytest

from chatbot.memory import MemoryStore


async def test_product_interactions_are_scoped_persistent_and_cascade(tmp_path):
    path = tmp_path / "memory.sqlite3"
    memory = await MemoryStore.open(path)
    try:
        turn = await memory.begin_interaction("alice", "one", "S3 returns AccessDenied", "s3")
        await memory.record_retrieval(
            turn,
            "S3 permission denied",
            "hybrid",
            [{"source": "aws/iam/permissions", "chunk": 2, "product_id": "iam", "score": 0.03}],
        )
        await memory.finish_interaction(turn, "completed")
        await memory.record_incident(
            "alice", "one", "s3", "AccessDenied", "open", "", "S3 returns AccessDenied"
        )
        history = await memory.history("alice")
        assert {(r["product_id"], r["relationship"]) for r in history["interactions"]} == {
            ("s3", "selected"),
            ("s3", "mentioned"),
            ("iam", "retrieved"),
        }
        assert history["incidents"][0]["status"] == "open"
        assert await memory.history("bob") == {"interactions": [], "incidents": []}
        await memory.record_incident(
            "bob", "one", "s3", "Other problem", "open", "", "Other problem"
        )
        await memory.record_incident(
            "alice",
            "one",
            "s3",
            "AccessDenied",
            "resolved",
            "Fixed IAM policy",
            "The policy fix worked",
        )
        assert (await memory.history("bob"))["incidents"][0]["status"] == "open"
        await memory.save("alice", "style", "brief", "preference", "Keep answers brief")
    finally:
        await memory.close()
    memory = await MemoryStore.open(path)
    try:
        assert (await memory.history("alice", "s3"))["incidents"][0][
            "resolution"
        ] == "Fixed IAM policy"
        assert all(
            r["product_id"] == "s3" for r in (await memory.history("alice", "s3"))["interactions"]
        )
        await memory.delete_session_history("alice", "one")
        assert await memory.history("alice") == {"interactions": [], "incidents": []}
        assert (await memory.list("alice"))[0]["value"] == "brief"
        async with memory.db.execute("SELECT count(*) FROM retrievals") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await memory.close()


async def test_invalid_incident_cannot_be_recorded(tmp_path):
    memory = await MemoryStore.open(tmp_path / "memory.db")
    try:
        with pytest.raises(ValueError):
            await memory.record_incident(
                "alice", "one", "s3", "Error", "resolved", "", "Suggested fix"
            )
        with pytest.raises(ValueError):
            await memory.record_incident("alice", "one", "invented", "Error", "open", "", "Error")
    finally:
        await memory.close()


async def test_resolution_feedback_and_source_attribution(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from google.genai import types

    from chatbot.agents import build_agents
    from chatbot.products import Turn, current_turn

    memory = await MemoryStore.open(tmp_path / "feedback.db")
    knowledge = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {
                    "source": "aws/s3",
                    "chunk": 0,
                    "text": "Check policy",
                    "product_id": "s3",
                    "score": 0.03,
                }
            ]
        )
    )
    root, connections = build_agents(
        SimpleNamespace(chat_model="openai/gpt-4.1-mini"), knowledge, memory
    )
    turn_id = await memory.begin_interaction("alice", "one", "S3 AccessDenied", "s3")
    turn = Turn(turn_id, "alice", "one", "s3", "S3 AccessDenied")
    token = current_turn.set(turn)
    try:
        await memory.record_incident(
            "alice", "one", "s3", "AccessDenied", "open", "", turn.question
        )
        await root.tools[0].agent.tools[0]("S3 AccessDenied")
        response = SimpleNamespace(
            partial=False, content=types.Content(parts=[types.Part(text="Check the policy.")])
        )
        await root.after_model_callback[1](None, response)
        assert "solve your issue" in response.content.parts[-1].text
        incident = (await memory.history("alice"))["incidents"][0]
        assert incident["assistance"][0]["type"] == "rag"
        assert incident["resolution_sources"] == []
        turn.question = "The local documentation helped. Adding GetObject permission fixed it."
        record = next(
            t for t in root.tools[2].agent.tools if getattr(t, "__name__", "") == "record_incident"
        )
        rejected = await record(
            "s3", "AccessDenied", "resolved", "Invented confirmation", "Fixed policy"
        )
        assert "error" in rejected
        result = await record(
            "s3",
            "AccessDenied",
            "resolved",
            turn.question,
            "Added GetObject permission",
            "rag",
            "solved",
        )
        assert result["status"] == "saved"
        incident = (await memory.history("alice"))["incidents"][0]
        assert incident["status"] == "resolved"
        assert incident["contribution"] == "solved"
        assert incident["resolution_sources"][0]["tool"] == "search_knowledge"
        response = SimpleNamespace(
            partial=False, content=types.Content(parts=[types.Part(text="Recorded.")])
        )
        await root.after_model_callback[1](None, response)
        assert len(response.content.parts) == 1
        assert (await memory.history("bob"))["incidents"] == []
    finally:
        current_turn.reset(token)
        await connections.close()
        await memory.close()


async def test_partial_help_and_unobserved_source_rejected(tmp_path):
    memory = await MemoryStore.open(tmp_path / "partial.db")
    try:
        await memory.record_incident("alice", "one", "s3", "Denied", "open", "", "Denied")
        await memory.record_assistance(
            "alice",
            "one",
            "s3",
            [{"type": "mcp", "tool": "aws___read_documentation", "interaction_id": "turn-one"}],
        )
        await memory.record_incident(
            "alice",
            "one",
            "s3",
            "Denied",
            "open",
            "",
            "The live docs helped but it still fails",
            "mcp",
            "helped",
        )
        incident = (await memory.history("alice"))["incidents"][0]
        assert incident["status"] == "open"
        assert incident["contribution"] == "helped"
        assert incident["helpful_source"] == "mcp"
        with pytest.raises(ValueError, match="not recorded"):
            await memory.record_incident(
                "alice", "one", "s3", "Denied", "resolved", "Fixed", "Fixed", "rag", "solved"
            )
    finally:
        await memory.close()


async def test_failed_mcp_does_not_receive_credit(tmp_path):
    from types import SimpleNamespace

    from chatbot.agents import build_agents
    from chatbot.products import Turn, current_turn

    memory = await MemoryStore.open(tmp_path / "mcp.db")
    root, connections = build_agents(
        SimpleNamespace(chat_model="openai/gpt-4.1-mini"), None, memory
    )
    turn = Turn("id", "alice", "session", "s3")
    token = current_turn.set(turn)
    callback = root.tools[1].agent.after_tool_callback
    try:
        tool = SimpleNamespace(name="inspect_aws_resource")
        await callback(tool, {}, None, {"content": [{"text": '{"error":"disabled"}'}]})
        await callback(tool, {}, None, {"isError": True})
        assert not turn.assistance
        await callback(
            SimpleNamespace(name="aws___read_documentation"),
            {},
            None,
            {"content": [{"text": "Official AWS documentation"}]},
        )
        assert turn.assistance[0]["type"] == "mcp"
    finally:
        current_turn.reset(token)
        await connections.close()
        await memory.close()
