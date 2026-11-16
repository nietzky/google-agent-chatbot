"""Create and idempotently seed the fictional notification index."""
import asyncio
import json
from pathlib import Path

from elasticsearch import AsyncElasticsearch

from chatbot.config import Settings


async def main():
    """Upsert ten demo records using stable IDs, without changing other indices."""
    settings = Settings()
    index = "cloudhelp-notifications-v1"
    async with AsyncElasticsearch(settings.elasticsearch_url, api_key=(
        settings.elasticsearch_api_key.get_secret_value()
        if settings.elasticsearch_api_key else None)) as es:
        if not await es.indices.exists(index=index):
            await es.indices.create(index=index, mappings={"dynamic": "strict", "properties": {
                "title": {"type": "text"}, "description": {"type": "text"},
                "product_id": {"type": "keyword"}, "severity": {"type": "keyword"},
                "created_at": {"type": "date"}, "demo": {"type": "boolean"},
            }})
        source = Path(__file__).resolve().parents[1] / "knowledge/notifications.json"
        for identifier, document in json.loads(source.read_text()).items():
            await es.index(index=index, id=identifier, document=document)
        await es.indices.refresh(index=index)
    print("Seeded 10 demo notifications into cloudhelp-notifications-v1")


if __name__ == "__main__":
    asyncio.run(main())
