"""Opt-in real Elasticsearch tests; no Google calls or credentials required."""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from elasticsearch import AsyncElasticsearch

from chatbot.retrieval import KnowledgeStore

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_ELASTICSEARCH_URL"),
    reason="Set TEST_ELASTICSEARCH_URL for integration tests",
)


async def test_live_ingestion_hybrid_and_replacement():
    index = "chatbot-test-" + uuid.uuid4().hex
    es = AsyncElasticsearch(os.environ["TEST_ELASTICSEARCH_URL"])
    settings = SimpleNamespace(
        elasticsearch_index=index, embedding_model="test", embedding_dimensions=3
    )
    store = KnowledgeStore(es, None, settings)

    async def vectors(texts):
        return [[1.0, 0.0, 0.0] if "vacation" in text else [0.0, 1.0, 0.0] for text in texts]

    store.embed = AsyncMock(side_effect=vectors)
    try:
        await store.ensure_index()
        await store.ensure_index()
        assert await store.ingest("handbook", "vacation policy " * 200, {"product_id": "s3"}) > 1
        assert (
            await store.ingest("travel", "Travel expenses are reimbursed.", {"product_id": "ec2"})
            == 1
        )
        hits = await store.search("vacation", "hybrid")
        assert hits[0]["source"] == "handbook"
        assert "embedding" not in hits[0]
        assert (await store.search("vacation", "keyword"))[0]["source"] == "handbook"
        assert (await store.search("vacation", "vector"))[0]["source"] == "handbook"
        for mode in ("keyword", "vector", "hybrid"):
            filtered = await store.search("vacation Travel", mode, product_id="ec2")
            assert filtered and all(h["product_id"] == "ec2" for h in filtered)
        await store.ingest("handbook", "vacation updated")
        count = await es.count(index=index, query={"term": {"source": "handbook"}})
        assert count["count"] == 1
    finally:
        await es.indices.delete(index=index, ignore_unavailable=True)
        await es.close()
