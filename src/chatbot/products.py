"""Stable product catalog and per-request context; identities never come from the model."""

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[2] / "knowledge/aws/products.json"


# Read the bundled UTF-8 product catalog, or return an empty list if it is absent. This does not
# query Elasticsearch or verify document availability.
def products():
    return json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else []


# Match catalog names and aliases case-insensitively with word boundaries and return each
# matching product ID once. Matches describe mentions, not confirmed ownership or usage.
def mentioned_products(text):
    result = []
    for product in products():
        names = [product["name"], *product.get("aliases", [])]
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) for name in names):
            result.append(product["id"])
    return result


@dataclass
class Turn:
    id: str
    user_id: str
    session_id: str
    product_id: str | None
    question: str = ""
    evidence: dict | None = None
    assistance: list[dict] = field(default_factory=list)


current_turn: ContextVar[Turn | None] = ContextVar("pingo_turn", default=None)
