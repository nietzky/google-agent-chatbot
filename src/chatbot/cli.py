import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from chatbot.agents import build_agents
from chatbot.config import Settings
from chatbot.learning import AutomaticMemory
from chatbot.products import Turn, current_turn
from chatbot.runtime import APP, resources


# Open or resume an ADK conversation and read terminal input without blocking the event loop.
# Run bounded agent turns, record interaction outcomes for automatic memory extraction, and
# close the worker, MCP connections, and session service on exit.
async def chat(settings, memory, knowledge, user, session_id):
    # ADK's LiteLLM adapter reads OpenAI credentials from the environment.
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key.get_secret_value()
    settings.session_db.parent.mkdir(parents=True, exist_ok=True)
    sessions = DatabaseSessionService(
        db_url=f"sqlite+aiosqlite:///{settings.session_db.as_posix()}"
    )
    learning = await AutomaticMemory(settings, knowledge.openai, sessions).__aenter__()
    root, tool_connections = build_agents(settings, knowledge, memory)
    runner = Runner(
        agent=root, app_name=APP, session_service=sessions, memory_service=memory.adk_service
    )
    session = await sessions.get_session(app_name=APP, user_id=user, session_id=session_id)
    if session is None:
        await sessions.create_session(app_name=APP, user_id=user, session_id=session_id)
    print(f"Session: {session_id} | User: {user}\nType /quit to exit.")
    try:
        while True:
            try:
                message = await asyncio.to_thread(input, "You > ")
            except EOFError:
                break
            if message.strip() == "/quit":
                break
            if not message.strip():
                continue
            if len(message) > 12000:
                print("Please keep each message under 12,000 characters.")
                continue
            turn_id = await memory.begin_interaction(user, session_id, message)
            token = current_turn.set(Turn(turn_id, user, session_id, None, message))
            outcome = "failed"
            try:
                async with asyncio.timeout(180):
                    async for event in runner.run_async(
                        user_id=user,
                        session_id=session_id,
                        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
                        run_config=RunConfig(max_llm_calls=20),
                    ):
                        if event.is_final_response() and event.content:
                            text = "".join(part.text or "" for part in event.content.parts or [])
                            if text:
                                print(f"{event.author} > {text}")
                outcome = "completed"
            except Exception as exc:
                print(f"Turn failed ({type(exc).__name__}). Check service availability and config.")
            finally:
                await memory.finish_interaction(turn_id, outcome)
                if outcome == "completed" and memory.adk_service:
                    await memory.adk_service.add_session_to_memory(
                        await sessions.get_session(
                            app_name=APP, user_id=user, session_id=session_id
                        )
                    )
                current_turn.reset(token)
    finally:
        await learning.__aexit__(None, None, None)
        await tool_connections.close()
        await sessions.close()


# Load settings and shared resources, then dispatch the selected CLI command. Validate text-file
# ingestion limits, print search or memory results, or start an interactive conversation.
async def run(args):
    settings = Settings()
    async with resources(settings) as (memory, knowledge):
        if args.command == "ingest":
            path = Path(args.path)
            if path.suffix.lower() not in {".md", ".txt"} or path.stat().st_size > 2_000_000:
                raise ValueError("Use a UTF-8 .md or .txt file no larger than 2 MB")
            await knowledge.ensure_index()
            count = await knowledge.ingest(
                args.source or path.name, path.read_text(encoding="utf-8")
            )
            print(f"Indexed {count} chunks")
        elif args.command == "search":
            print(json.dumps(await knowledge.search(args.query, args.mode), indent=2))
        elif args.command == "memories":
            print(json.dumps(await memory.list(args.user), indent=2))
        elif args.command == "forget":
            print(await memory.forget(args.user, args.key))
        else:
            await chat(settings, memory, knowledge, args.user, args.session or str(uuid.uuid4()))


# Define CLI subcommands and arguments, parse the process arguments, and enter the asynchronous
# command dispatcher with asyncio.run.
def main():
    parser = argparse.ArgumentParser(description="Cloud Help IT support with Google ADK")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_parser = sub.add_parser("chat")
    chat_parser.add_argument("--user", default="local-user")
    chat_parser.add_argument("--session")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("path")
    ingest.add_argument("--source", help="Stable unique document name; reuse to replace it")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=["hybrid", "keyword", "vector"], default="hybrid")
    for name in ("memories", "forget"):
        command = sub.add_parser(name)
        command.add_argument("--user", default="local-user")
        if name == "forget":
            command.add_argument("key")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
