"""Shared connection lifecycle for the CLI and HTTP service."""

from contextlib import AsyncExitStack, asynccontextmanager

from elasticsearch import AsyncElasticsearch
from openai import AsyncOpenAI

from chatbot.adk_memory import CloudHelpMemoryService
from chatbot.memory import MemoryStore
from chatbot.retrieval import KnowledgeStore

APP = "sample_chatbot"


# Open the SQLite store and reusable Elasticsearch/OpenAI clients once for a CLI command or
# service lifetime. Yield memory and retrieval services and close all registered resources in
# reverse order, including on exceptions.
@asynccontextmanager
async def resources(settings):
    if settings.memory_index == settings.elasticsearch_index:
        raise ValueError(
            "Memory and product documentation must use different Elasticsearch indices"
        )
    async with AsyncExitStack() as stack:
        memory = await MemoryStore.open(settings.memory_db)
        stack.push_async_callback(memory.close)
        es = AsyncElasticsearch(
            settings.elasticsearch_url,
            api_key=(
                settings.elasticsearch_api_key.get_secret_value()
                if settings.elasticsearch_api_key
                else None
            ),
            request_timeout=20,
            max_retries=2,
            retry_on_timeout=True,
        )
        stack.push_async_callback(es.close)
        client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=30.0,
            max_retries=2,
        )
        stack.push_async_callback(client.close)
        memory.adk_service = CloudHelpMemoryService(memory, APP, es, settings.memory_index)
        stack.push_async_callback(memory.adk_service.close)
        await memory.adk_service.start(settings.memory_db)
        yield memory, KnowledgeStore(es, client, settings)
