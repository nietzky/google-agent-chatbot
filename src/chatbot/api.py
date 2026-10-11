"""Local, single-user FastAPI transport. Run with one Uvicorn worker."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chatbot.agents import build_agents
from chatbot.config import Settings
from chatbot.learning import AutomaticMemory, learning_status
from chatbot.products import Turn, current_turn, products
from chatbot.runtime import APP, resources

PROJECT = Path(__file__).resolve().parents[2]
ORIGINS = {f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (8000, 5173)}
logger = logging.getLogger(__name__)


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=12000)
    product_id: str | None = Field(default=None, max_length=100)


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="New conversation", min_length=1, max_length=100)


class ForgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)


# Convert persisted ADK events into user-visible messages. Exclude partial events, tool
# exchanges, thought text, and specialist responses; retain user text and final orchestrator
# answers.
def public_messages(session):
    """Expose user text and final orchestrator answers, never prompts or tool payloads."""
    messages = []
    for event in session.events:
        if not event.content or event.partial:
            continue
        if event.author != "user" and not (
            event.author == "orchestrator" and event.is_final_response()
        ):
            continue
        text = "".join(part.text or "" for part in event.content.parts or [] if not part.thought)
        if text:
            messages.append(
                {
                    "id": event.id,
                    "role": "user" if event.author == "user" else "assistant",
                    "text": text,
                }
            )
    return messages


class ChatService:
    # Store shared service dependencies and the configured backend user. Track active sessions
    # in memory to prevent overlapping turns and bound concurrency within this process.
    def __init__(self, settings, memory, knowledge, sessions, agent_factory=build_agents):
        self.settings, self.memory, self.knowledge = settings, memory, knowledge
        self.sessions, self.agent_factory = sessions, agent_factory
        self.user = settings.web_user_id
        self.active: set[str] = set()

    # Look up an ADK session using both its ID and the configured user identity. Raise HTTP 404
    # when that user has no matching session.
    async def get_session(self, session_id):
        session = await self.sessions.get_session(
            app_name=APP, user_id=self.user, session_id=session_id
        )
        if session is None:
            raise HTTPException(404, "Conversation not found")
        return session

    # Synchronously reserve a session before streaming starts. Reject duplicate turns with HTTP
    # 409 and reject new work with HTTP 429 when three sessions are already active.
    def reserve(self, session_id):
        # No await between check and insertion: atomic within this process's event loop.
        if session_id in self.active:
            raise HTTPException(409, "This conversation is already generating a response")
        if len(self.active) >= 3:
            raise HTTPException(429, "The assistant is busy. Try again shortly.")
        self.active.add(session_id)

    # Bridge an ADK producer task to an SSE response through an asyncio queue. Emit heartbeat
    # comments while waiting and always cancel unfinished work and release the session
    # reservation when the stream closes.
    async def stream(self, session_id, text, product_id=None):
        queue = asyncio.Queue()

        # Record the interaction, establish turn-local context, and run the orchestrator with
        # time and model-call limits. Queue status and answer events, mark successful completion
        # to enqueue memory extraction, and clean up MCP connections and context on every exit.
        async def produce():
            weather = None
            turn_id = None
            token = None
            outcome = "failed"
            try:
                turn_id = await self.memory.begin_interaction(
                    self.user, session_id, text, product_id
                )
                token = current_turn.set(Turn(turn_id, self.user, session_id, product_id, text))
                root, weather = self.agent_factory(self.settings, self.knowledge, self.memory)
                runner = Runner(
                    agent=root,
                    app_name=APP,
                    session_service=self.sessions,
                    memory_service=getattr(self.memory, "adk_service", None),
                )
                await queue.put(("status", {"label": "Thinking about your question"}))
                answered = False
                async with asyncio.timeout(180):
                    async for event in runner.run_async(
                        user_id=self.user,
                        session_id=session_id,
                        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
                        run_config=RunConfig(max_llm_calls=20),
                    ):
                        for call in event.get_function_calls():
                            labels = {
                                "knowledge_agent": "Searching AWS product documentation",
                                "aws_support_agent": "Checking AWS documentation and diagnostics",
                                "memory_agent": "Working with your saved preferences",
                                "azure_knowledge_agent": "Searching Microsoft and Azure documentation",
                            }
                            if call.name in labels:
                                await queue.put(("status", {"label": labels[call.name]}))
                        if event.error_code:
                            raise RuntimeError("Agent returned an error")
                        if (
                            event.author == "orchestrator"
                            and event.is_final_response()
                            and event.content
                        ):
                            answer = "".join(
                                p.text or "" for p in event.content.parts or [] if not p.thought
                            )
                            if answer:
                                answered = True
                                await queue.put(
                                    (
                                        "message",
                                        {"id": event.id, "role": "assistant", "text": answer},
                                    )
                                )
                if not answered:
                    raise RuntimeError("Agent returned no answer")
                outcome = "completed"
                # SQLite trigger queues extraction atomically with completion, before SSE ends.
                await self.memory.finish_interaction(turn_id, outcome)
                if getattr(self.memory, "adk_service", None):
                    await self.memory.adk_service.add_session_to_memory(
                        await self.get_session(session_id)
                    )
                if self.memory.learning:
                    self.memory.learning.wake.set()
                turn_id = None
                await queue.put(("done", {}))
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception as exc:
                # Exception strings from providers may contain sensitive request information.
                logger.warning("Chat turn failed: %s", type(exc).__name__)
                await queue.put(
                    ("error", {"message": "The response could not be completed. Please try again."})
                )
            finally:
                try:
                    if weather:
                        await weather.close()
                finally:
                    try:
                        if turn_id:
                            await self.memory.finish_interaction(turn_id, outcome)
                    finally:
                        if token is not None:
                            current_turn.reset(token)
                        await queue.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                event, payload = item
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.active.discard(session_id)


# Construct the FastAPI application, routes, origin checks, and built React asset serving. An
# injected ChatService lets tests use controlled dependencies without opening production
# connections.
def create_app(service=None):
    # Use an injected service as-is, or open shared database, search, model, and ADK session
    # resources for the application lifetime. Start automatic memory extraction and close owned
    # resources during shutdown.
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.chat = service
            yield
            return
        settings = Settings()
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key.get_secret_value()
        settings.session_db.parent.mkdir(parents=True, exist_ok=True)
        async with resources(settings) as (memory, knowledge):
            sessions = DatabaseSessionService(
                db_url=f"sqlite+aiosqlite:///{settings.session_db.as_posix()}"
            )
            try:
                async with AutomaticMemory(settings, knowledge.openai, sessions) as learning:
                    memory.learning = learning
                    app.state.chat = ChatService(settings, memory, knowledge, sessions)
                    yield
            finally:
                await sessions.close()

    app = FastAPI(title="Pingo Support API", version="0.4.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ORIGINS),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    # Reject browser requests whose supplied Origin is outside the allowed local frontend
    # addresses. Requests without an Origin continue; this local-development check is not user
    # authentication.
    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in ORIGINS:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        return await call_next(request)

    # Read the shared ChatService from application state so route handlers use the same
    # connections and concurrency reservations.
    def chat(request):
        return request.app.state.chat

    # Return a lightweight process-liveness response. This endpoint does not probe
    # Elasticsearch, SQLite, MCP, or the model provider.
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # Combine local AWS manifest metadata with Elasticsearch source and chunk counts. Bound the
    # lookup with a timeout and report unavailable data when search cannot respond.
    @app.get("/api/library")
    async def library(request: Request):
        service = chat(request)
        metadata = {}
        manifest = PROJECT / "knowledge/aws/manifest.json"
        if manifest.exists():
            metadata = {
                d["source_id"]: d
                for d in json.loads(manifest.read_text(encoding="utf-8"))["documents"]
            }
        try:
            async with asyncio.timeout(5):
                result = await service.knowledge.es.search(
                    index=service.knowledge.index,
                    size=0,
                    aggs={"sources": {"terms": {"field": "source", "size": 1000}}},
                )
            sources = [
                {
                    "id": b["key"],
                    "title": metadata.get(b["key"], {}).get("title", b["key"].replace("_", " ")),
                    "url": metadata.get(b["key"], {}).get("url"),
                    "chunks": b["doc_count"],
                }
                for b in result["aggregations"]["sources"]["buckets"]
            ]
            return {
                "available": True,
                "sources": sources,
                "chunks": sum(s["chunks"] for s in sources),
            }
        except Exception:
            return {"available": False, "sources": [], "chunks": 0}

    # Read indexed product IDs and chunk counts from Elasticsearch and join them to the local
    # catalog. Return only indexed catalog products, or an unavailable response on lookup
    # failure.
    @app.get("/api/products")
    async def supported_products(request: Request):
        service = chat(request)
        try:
            async with asyncio.timeout(5):
                result = await service.knowledge.es.search(
                    index=service.knowledge.index,
                    size=0,
                    aggs={"products": {"terms": {"field": "product_id", "size": 1000}}},
                )
            counts = {
                b["key"]: b["doc_count"] for b in result["aggregations"]["products"]["buckets"]
            }
            return {
                "available": True,
                "products": [
                    {**p, "chunks": counts[p["id"]]} for p in products() if p["id"] in counts
                ],
            }
        except Exception:
            return {"available": False, "products": []}

    # Return the configured user's SQLite product interactions and incidents, optionally
    # filtered by product ID.
    @app.get("/api/product-history")
    async def product_history(request: Request, product_id: str | None = None):
        service = chat(request)
        return await service.memory.history(service.user, product_id)

    # List the configured user's ADK sessions in newest-first order with IDs, titles, and update
    # times for the history picker.
    @app.get("/api/conversations")
    async def conversations(request: Request):
        service = chat(request)
        result = await service.sessions.list_sessions(app_name=APP, user_id=service.user)
        ordered = sorted(result.sessions, key=lambda s: s.last_update_time, reverse=True)
        return [
            {
                "id": s.id,
                "title": s.state.get("title", "Conversation"),
                "updated_at": s.last_update_time,
            }
            for s in ordered
        ]

    # Create a UUID-backed ADK session for the configured user and persist the supplied title in
    # session state. Return its metadata for subsequent message requests.
    @app.post("/api/conversations", status_code=201)
    async def create_conversation(body: ConversationInput, request: Request):
        service = chat(request)
        session = await service.sessions.create_session(
            app_name=APP,
            user_id=service.user,
            session_id=str(uuid.uuid4()),
            state={"title": body.title},
        )
        return {"id": session.id, "title": body.title, "updated_at": session.last_update_time}

    # Verify session ownership, then project its ADK events into the public conversation
    # transcript.
    @app.get("/api/conversations/{session_id}/messages")
    async def messages(session_id: str, request: Request):
        return public_messages(await chat(request).get_session(str(session_id)))

    # Validate session ownership and the optional catalog product, then reserve a turn and
    # return its SSE stream. A background cleanup guard releases the reservation when the
    # response finishes.
    @app.post("/api/conversations/{session_id}/messages")
    async def send(session_id: str, body: MessageInput, request: Request):
        service = chat(request)
        identifier = str(session_id)
        await service.get_session(identifier)
        if body.product_id and body.product_id not in {p["id"] for p in products()}:
            raise HTTPException(422, "Unknown product")
        service.reserve(identifier)
        return StreamingResponse(
            service.stream(identifier, body.text, body.product_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(service.active.discard, identifier),
        )

    # Verify ownership and reject deletion during an active turn. Delete SQLite product history
    # and its derived data, then remove the ADK session; these separate stores do not share one
    # transaction.
    @app.delete("/api/conversations/{session_id}", status_code=204)
    async def delete_conversation(session_id: str, request: Request):
        service = chat(request)
        identifier = str(session_id)
        await service.get_session(identifier)
        if identifier in service.active:
            raise HTTPException(409, "Stop the response before deleting this conversation")
        await service.memory.delete_session_history(service.user, identifier)
        await service.sessions.delete_session(
            app_name=APP, user_id=service.user, session_id=identifier
        )

    # Return whether automatic extraction is enabled and the current user's extraction-job
    # counts grouped by state.
    @app.get("/api/memories/status")
    async def memory_status(request: Request):
        service = chat(request)
        return {
            "enabled": service.settings.auto_memory_enabled,
            "jobs": await learning_status(service.memory, service.user),
            "memory_service": "PingoMemoryService (Google ADK)",
            "search_index": service.settings.memory_index,
            "pending_index_updates": (
                await (
                    await service.memory.db.execute(
                        "SELECT count(*) FROM memory_search_jobs WHERE user_id=?", (service.user,)
                    )
                ).fetchone()
            )[0],
        }

    # List the configured user's active long-term memories; expired facts remain in SQLite but
    # are omitted from this response.
    @app.get("/api/memories")
    async def memories(request: Request):
        return await chat(request).memory.list(chat(request).user)

    # Delete one memory key for the configured user and record its automatic-relearning
    # suppression marker.
    @app.delete("/api/memories")
    async def forget(body: ForgetInput, request: Request):
        return await chat(request).memory.forget(chat(request).user, body.key)

    dist = PROJECT / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    # Serve the built React entry page. Return HTTP 503 with build instructions when the
    # frontend distribution is missing.
    @app.get("/", include_in_schema=False)
    async def index():
        if not (dist / "index.html").exists():
            return JSONResponse(
                {"message": "Build the frontend with npm run build in frontend/"}, status_code=503
            )
        return FileResponse(dist / "index.html")

    return app


app = create_app()
