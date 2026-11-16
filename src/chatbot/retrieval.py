"""Elasticsearch BM25 + approximate kNN, fused without a paid RRF feature."""

import asyncio
import hashlib
import math

from elasticsearch.helpers import async_bulk


# Split text into overlapping character windows, trimming and omitting empty slices. Validate
# that overlap is nonnegative and smaller than the window; sizes are characters, not model
# tokens.
def chunks(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    if not 0 <= overlap < size:
        raise ValueError("Require 0 <= overlap < size")
    result = []
    for start in range(0, len(text), size - overlap):
        part = text[start : start + size].strip()
        if part:
            result.append(part)
        if start + size >= len(text):
            break
    return result


# Merge ranked result lists by document ID using reciprocal-rank fusion, summing 1/(constant +
# rank). Sort by fused score with deterministic ID ties and return only the requested number of
# source records.
def fuse(rankings, limit=5, constant=60):
    scores, documents = {}, {}
    for hits in rankings:
        for rank, hit in enumerate(hits, 1):
            identifier = hit["_id"]
            documents[identifier] = hit["_source"]
            scores[identifier] = scores.get(identifier, 0) + 1 / (constant + rank)
    return [
        dict(id=identifier, score=scores[identifier], **documents[identifier])
        for identifier in sorted(scores, key=lambda k: (-scores[k], k))[:limit]
    ]


class KnowledgeStore:
    # Store shared Elasticsearch and OpenAI clients plus index settings without making network
    # calls. Cache successful mapping validation for later vector queries.
    def __init__(self, es, openai_client, settings):
        self.es, self.openai, self.settings = es, openai_client, settings
        self.index = settings.elasticsearch_index
        self._index_verified = False

    # Return the embedding model and dimension fingerprint stored with the index so incompatible
    # vectors cannot silently share a search space.
    def _embedding_metadata(self):
        return {
            "embedding_model": self.settings.embedding_model,
            "dimensions": self.settings.embedding_dimensions,
        }

    # Fetch and compare index embedding metadata with current settings, raising on mismatch.
    # Cache a successful validation to avoid repeating this network lookup on every search.
    async def validate_index(self):
        """Do not compare embeddings from different models, even at the same dimension."""
        if not self._index_verified:
            mapping = await self.es.indices.get_mapping(index=self.index)
            # Elasticsearch returns concrete names even when queried through an alias.
            if len(mapping) != 1:
                raise ValueError("Knowledge index must resolve to exactly one concrete index")
            if next(iter(mapping.values()))["mappings"].get("_meta") != self._embedding_metadata():
                raise ValueError("Embedding configuration differs; use a new index and re-ingest")
            self._index_verified = True

    # Validate an existing index or create one with text, product metadata, and indexed cosine-
    # vector fields. Store the embedding fingerprint and mark the mapping verified for this
    # store instance.
    async def ensure_index(self):
        expected = self._embedding_metadata()
        if await self.es.indices.exists(index=self.index):
            await self.validate_index()
            return
        await self.es.indices.create(
            index=self.index,
            mappings={
                "_meta": expected,
                "properties": {
                    "source": {"type": "keyword"},
                    "text": {"type": "text"},
                    "product_id": {"type": "keyword"},
                    "product_name": {"type": "keyword"},
                    "url": {"type": "keyword"},
                    "title": {"type": "text"},
                    "chunk": {"type": "integer"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": expected["dimensions"],
                        "index": True,
                        "similarity": "cosine",
                    },
                },
            },
            settings={"number_of_shards": 1, "number_of_replicas": 0},
        )
        self._index_verified = True

    # Request embeddings from OpenAI and restore input order using response indices. Validate
    # count, dimensions, finite values, and nonzero norms, then return normalized vectors
    # aligned with the original texts.
    async def embed(self, texts):
        result = await self.openai.embeddings.create(
            model=self.settings.embedding_model,
            input=texts,
            dimensions=self.settings.embedding_dimensions,
            encoding_format="float",
        )
        vectors = []
        ordered = sorted(result.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise ValueError("Embedding response indices do not match inputs")
        for embedding in ordered:
            values = embedding.embedding
            norm = math.sqrt(sum(x * x for x in values))
            if (
                not math.isfinite(norm)
                or not norm
                or len(values) != self.settings.embedding_dimensions
            ):
                raise ValueError("Invalid embedding returned by OpenAI")
            vectors.append([x / norm for x in values])
        if len(vectors) != len(texts):
            raise ValueError("Embedding count mismatch")
        return vectors

    # Chunk and embed a nonempty document in bounded batches, then bulk upsert deterministic
    # source/chunk IDs with metadata. Delete obsolete trailing chunks when a replacement becomes
    # shorter; bulk writes and cleanup are separate operations.
    async def ingest(self, source: str, text: str, metadata: dict | None = None):
        parts = chunks(text)
        if not parts:
            raise ValueError("Document is empty")
        # Embed everything before mutating the index; bound each API batch.
        vectors = []
        for start in range(0, len(parts), 32):
            vectors.extend(await self.embed(parts[start : start + 32]))
        actions = [
            {
                "_index": self.index,
                "_id": hashlib.sha256(f"{source}:{i}".encode()).hexdigest(),
                "_source": {
                    **(metadata or {}),
                    "source": source,
                    "chunk": i,
                    "text": part,
                    "embedding": vector,
                },
            }
            for i, (part, vector) in enumerate(zip(parts, vectors))
        ]
        await async_bulk(self.es, actions, chunk_size=100, refresh="wait_for")
        # Remove obsolete tail chunks when a document becomes shorter.
        await self.es.delete_by_query(
            index=self.index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"source": source}},
                        {"range": {"chunk": {"gte": len(parts)}}},
                    ]
                }
            },
            refresh=True,
        )
        return len(parts)

    # Validate the query, mode, and result limit, then run keyword and/or vector retrieval with
    # an optional product filter. Run both branches concurrently for hybrid search and fuse
    # their ranked results.
    async def search(
        self, query: str, mode: str = "hybrid", limit: int = 5, product_id: str | None = None
    ):
        if mode not in {"hybrid", "keyword", "vector"}:
            raise ValueError("mode must be hybrid, keyword or vector")
        if not query.strip() or len(query) > 4000 or not 1 <= limit <= 10:
            raise ValueError("Query must be 1-4000 characters and limit 1-10")
        window = 30

        # Retrieve up to thirty BM25 text matches, optionally restricted to one product. Exclude
        # embedding arrays from returned sources; this branch makes no model-provider call.
        async def keyword():
            return await self.es.search(
                index=self.index,
                size=window,
                query={
                    "bool": {
                        "must": [{"match": {"text": query}}],
                        "filter": [{"term": {"product_id": product_id}}] if product_id else [],
                    }
                },
                source_excludes=["embedding"],
            )

        # Validate embedding compatibility, embed the query, and request approximate nearest
        # neighbors with a product prefilter when selected. Return source fields without the
        # large embedding arrays.
        async def vector():
            await self.validate_index()
            embedding = (await self.embed([query]))[0]
            return await self.es.search(
                index=self.index,
                size=window,
                knn={
                    "field": "embedding",
                    "query_vector": embedding,
                    "k": window,
                    "num_candidates": 100,
                    **({"filter": {"term": {"product_id": product_id}}} if product_id else {}),
                },
                source_excludes=["embedding"],
            )

        tasks = (
            [keyword(), vector()]
            if mode == "hybrid"
            else [keyword() if mode == "keyword" else vector()]
        )
        responses = await asyncio.gather(*tasks)
        return fuse([r["hits"]["hits"] for r in responses], limit)
