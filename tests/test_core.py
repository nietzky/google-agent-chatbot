from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatbot.memory import MemoryStore
from chatbot.retrieval import KnowledgeStore, chunks, fuse


def test_chunks_cover_text_and_validate_overlap():
    assert chunks("abcdefghij", 6, 2) == ["abcdef", "efghij"]
    assert chunks("  ") == []
    with pytest.raises(ValueError):
        chunks("abc", 3, 3)


def test_rrf_rewards_agreement_and_deduplicates():
    def hit(key):
        return {"_id": key, "_source": {"text": key}}

    result = fuse([[hit("a"), hit("b")], [hit("c"), hit("b")]], 3)
    assert [r["id"] for r in result] == ["b", "a", "c"]
    assert result[0]["score"] == pytest.approx(2 / 62)


async def test_memory_persistence_isolation_update_and_delete(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = await MemoryStore.open(path)
    try:
        await store.save("alice", "units", "celsius", "preference", "I use Celsius")
        await store.save("bob", "units", "fahrenheit", "preference", "I use Fahrenheit")
        await store.save("alice", "units", "fahrenheit", "preference", "Use Fahrenheit now")
        assert len(await store.list("alice")) == 1
        assert (await store.list("alice"))[0]["value"] == "fahrenheit"
        assert await store.forget("alice", "units") == {"deleted": 1}
        assert len(await store.list("bob")) == 1
        with pytest.raises(ValueError):
            await store.save("bob", "", "x", "preference", "x")
    finally:
        await store.close()
    reopened = await MemoryStore.open(path)
    try:
        assert len(await reopened.list("bob")) == 1
        assert await reopened.list("alice") == []
    finally:
        await reopened.close()


async def test_keyword_does_not_embed_and_hybrid_runs_both():
    es = SimpleNamespace(search=AsyncMock(return_value={"hits": {"hits": []}}))
    store = KnowledgeStore(es, None, SimpleNamespace(elasticsearch_index="test"))
    store.validate_index = AsyncMock()
    store.embed = AsyncMock(return_value=[[1, 0]])
    await store.search("vacation", "keyword")
    store.embed.assert_not_called()
    await store.search("vacation", "hybrid")
    store.embed.assert_awaited_once_with(["vacation"])
    assert es.search.await_count == 3
    assert es.search.call_args.kwargs["knn"]["num_candidates"] == 100
    with pytest.raises(ValueError):
        await store.search("vacation", "invalid")


async def test_embedding_normalization_and_index_compatibility():
    embed = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[3.0, 4.0])])
    )
    es = SimpleNamespace(
        indices=SimpleNamespace(
            exists=AsyncMock(return_value=True),
            get_mapping=AsyncMock(return_value={"test": {"mappings": {"_meta": {}}}}),
        )
    )
    client = SimpleNamespace(embeddings=SimpleNamespace(create=embed))
    settings = SimpleNamespace(
        elasticsearch_index="test", embedding_model="test-model", embedding_dimensions=2
    )
    store = KnowledgeStore(es, client, settings)
    assert await store.embed(["hello"]) == [[0.6, 0.8]]
    embed.assert_awaited_once_with(
        model="test-model", input=["hello"], dimensions=2, encoding_format="float"
    )
    with pytest.raises(ValueError, match="new index"):
        await store.ensure_index()
    with pytest.raises(ValueError, match="new index"):
        await store.search("hello", "vector")
    assert embed.await_count == 1


async def test_openai_embedding_order_and_invalid_vectors():
    embed = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 2.0]),
                SimpleNamespace(index=0, embedding=[3.0, 0.0]),
            ]
        )
    )
    client = SimpleNamespace(embeddings=SimpleNamespace(create=embed))
    store = KnowledgeStore(
        None,
        client,
        SimpleNamespace(
            elasticsearch_index="test",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=2,
        ),
    )
    assert await store.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    embed.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[float("nan"), 1])]
    )
    with pytest.raises(ValueError, match="Invalid embedding"):
        await store.embed(["bad vector"])
    embed.return_value = SimpleNamespace(data=[SimpleNamespace(index=2, embedding=[1, 0])])
    with pytest.raises(ValueError, match="indices"):
        await store.embed(["missing input"])
