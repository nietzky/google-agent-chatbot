"""Rebuild the AWS index from included snapshots, without scraping again.

Requires OpenAI embedding access and Elasticsearch. Validates snapshot hashes first.
"""

import asyncio
import hashlib
import json
from pathlib import Path

from chatbot.config import Settings
from chatbot.runtime import resources


# Load saved AWS pages and verify every snapshot hash before writing to search. Ensure the
# configured index exists, then re-embed and restore pages with bounded concurrency without
# scraping AWS again.
async def main():
    root = Path("knowledge/aws")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    documents = []
    for doc in manifest["documents"]:
        path = root / f"{doc['product_id']}-{doc['source_id'].rsplit('/', 1)[1]}.txt"
        text = path.read_text(encoding="utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != doc["sha256"]:
            raise ValueError(f"Snapshot hash mismatch: {path.name}")
        documents.append((doc, text))
    async with resources(Settings()) as (_, knowledge):
        await knowledge.ensure_index()
        semaphore = asyncio.Semaphore(3)

        # Wait for a restore concurrency slot, rebuild the document metadata, and ingest one
        # verified snapshot. Return its indexed chunk count for the final restore total.
        async def ingest(doc, text):
            async with semaphore:
                metadata = {k: doc[k] for k in ("product_id", "product_name", "title", "url")}
                return await knowledge.ingest(doc["source_id"], text, metadata)

        counts = await asyncio.gather(*(ingest(doc, text) for doc, text in documents))
        print(f"Restored {len(documents)} pages / {sum(counts)} chunks for 100 AWS products")


if __name__ == "__main__":
    asyncio.run(main())
