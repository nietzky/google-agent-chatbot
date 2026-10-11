import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.adk.sessions import DatabaseSessionService
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from chatbot.agents import build_agents


async def test_agent_wiring_and_dynamic_memory():
    memory = SimpleNamespace(
        list=AsyncMock(return_value=[{"key": "units", "value": "celsius", "kind": "preference"}])
    )
    root, mcp_tools = build_agents(SimpleNamespace(chat_model="openai/gpt-4.1-mini"), None, memory)
    try:
        assert root.name == "orchestrator"
        assert root.model.model == "openai/gpt-4.1-mini"
        assert all(tool.agent.model.model == "openai/gpt-4.1-mini" for tool in root.tools[:3])
        assert [tool.agent.name for tool in root.tools[:3]] == [
            "knowledge_agent",
            "aws_support_agent",
            "memory_agent",
        ]
        assert root.tools[3].agent.name == "azure_knowledge_agent"
        assert root.tools[3].agent.tools[0].tool_filter == [
            "microsoft_docs_search",
            "microsoft_docs_fetch",
            "microsoft_code_sample_search",
        ]
        assert "celsius" in await root.instruction(SimpleNamespace(user_id="alice"))
        memory.list.assert_awaited_once_with("alice")
        assert root.tools[1].agent.tools[0].tool_filter == [
            "aws___search_documentation",
            "aws___read_documentation",
            "aws___list_regions",
            "aws___get_regional_availability",
        ]
    finally:
        await mcp_tools.close()


async def test_knowledge_tool_supplies_explicit_source_citations():
    knowledge = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                {"id": "internal-hash", "source": "handbook", "chunk": 0, "text": "25 days"}
            ]
        )
    )
    root, mcp_tools = build_agents(
        SimpleNamespace(chat_model="openai/gpt-4.1-mini"), knowledge, None
    )
    try:
        result = await root.tools[0].agent.tools[0]("vacation")
        assert result == {
            "results": [{"citation": "[handbook#0]", "text": "25 days", "product_id": None}]
        }
    finally:
        await mcp_tools.close()


async def test_real_mcp_stdio_round_trip():
    params = StdioServerParameters(command=sys.executable, args=["-m", "chatbot.aws_tools_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "diagnostics_status",
                "inspect_aws_resource",
            }
            result = await session.call_tool("diagnostics_status", {})
            assert '"enabled"' in result.content[0].text


async def test_adk_database_session_persists(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
    service = DatabaseSessionService(db_url=url)
    try:
        await service.create_session(app_name="test", user_id="alice", session_id="one")
        assert await service.get_session(app_name="test", user_id="alice", session_id="one")
        assert await service.get_session(app_name="test", user_id="bob", session_id="one") is None
    finally:
        await service.close()
