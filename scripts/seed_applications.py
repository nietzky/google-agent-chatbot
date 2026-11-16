"""Idempotently populate the fictional application catalog."""
import asyncio

from elasticsearch import AsyncElasticsearch

from chatbot.applications import seed_applications
from chatbot.config import Settings


async def main():
    """Connect using local configuration and upsert ten two-field catalog documents."""
    settings = Settings()
    async with AsyncElasticsearch(settings.elasticsearch_url, api_key=(
        settings.elasticsearch_api_key.get_secret_value()
        if settings.elasticsearch_api_key else None)) as es:
        await seed_applications(es)
    print("Seeded 10 fictional applications into cloudhelp-applications-v1")


if __name__ == "__main__":
    asyncio.run(main())
