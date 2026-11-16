"""Recommendation eligibility and user-scoped evidence regression tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatbot.applications import eligible_facts, suggested_applications


def test_only_supported_interest_and_usage_are_eligible():
    facts = [dict(category=c, product_id="s3", evidence="I use S3")
             for c in ["interest", "product_usage", "symptom", "preference"]]
    assert len(eligible_facts(facts)) == 2
    assert not eligible_facts([dict(category="interest", product_id="s3", evidence="")])
    assert not eligible_facts([dict(category="product_usage", evidence="I use S3")])


@pytest.mark.asyncio
async def test_exact_product_join_and_preserved_evidence():
    fact = dict(category="product_usage", product_id="s3", evidence="I use S3", key="usage")
    memory = SimpleNamespace(list=AsyncMock(return_value=[fact]))
    es = SimpleNamespace(search=AsyncMock(return_value={"hits": {"hits": [
        {"_id": "s3", "_source": {"name": "Bucket Beacon", "description": "Demo"}}]}}))
    result = await suggested_applications(es, memory, "user-a")
    memory.list.assert_awaited_once_with("user-a")
    assert es.search.call_args.kwargs["query"] == {"ids": {"values": ["s3"]}}
    assert result["applications"][0]["evidence"] == [fact]
    memory.list.return_value = []
    es.search.reset_mock()
    assert (await suggested_applications(es, memory, "user-a"))["applications"] == []
    es.search.assert_not_awaited()
