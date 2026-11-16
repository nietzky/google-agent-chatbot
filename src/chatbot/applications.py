"""Evidence-backed fictional app recommendations; SQLite remains the preference authority."""

import asyncio
import json
from pathlib import Path

INDEX = "cloudhelp-applications-v1"
CATALOG = Path(__file__).resolve().parents[2] / "knowledge/applications.json"


async def seed_applications(es):
    """Create the dedicated two-field index and idempotently seed ten demo documents.

    Document IDs are AWS product IDs; source documents contain only name and description.
    This explicit setup operation never runs in the latency-sensitive recommendation path.
    """
    if not await es.indices.exists(index=INDEX):
        await es.indices.create(index=INDEX, mappings={"dynamic": "strict", "properties": {
            "name": {"type": "text"}, "description": {"type": "text"},
        }})
    for identifier, document in json.loads(CATALOG.read_text()).items():
        await es.index(index=INDEX, id=identifier, document=document)
    await es.indices.refresh(index=INDEX)


def eligible_facts(facts):
    """Keep explicit interest/usage facts with a product and a nonempty supporting quote.

    Callers supply only the current user's active SQLite memories. Search mentions, symptoms,
    and unrelated preferences are not evidence of product usage or interest.
    """
    return [fact for fact in facts if fact.get("category") in {"interest", "product_usage"}
            and fact.get("product_id") and (fact.get("evidence") or "").strip()]


async def suggested_applications(es, memory, user):
    """Join active user facts to indexed apps by exact product ID, returning up to three.

    Facts retain their source references and timestamps. Newest memories rank first. No model
    invents recommendations, no browsing event creates a preference, and no shared cache can
    mix users. Forgetting or expiry removes eligibility on the next request.
    """
    facts = eligible_facts(await memory.list(user))
    if not facts:
        return {"available": True, "applications": []}
    async with asyncio.timeout(5):
        result = await es.search(index=INDEX, size=10,
                                 query={"ids": {"values": list(dict.fromkeys(
                                     fact["product_id"] for fact in facts))}})
    documents = {hit["_id"]: hit["_source"] for hit in result["hits"]["hits"]}
    suggestions = []
    for product_id in dict.fromkeys(fact["product_id"] for fact in facts):
        if product_id in documents:
            suggestions.append({**documents[product_id], "id": product_id,
                                "url": f"/applications/{product_id}",
                                "evidence": [fact for fact in facts
                                             if fact["product_id"] == product_id]})
    return {"available": True, "applications": suggestions[:3]}
