# Pingo â€” IT support with AWS product knowledge

Pingo is a local React + FastAPI support chatbot powered by **Google ADK** agents and
**OpenAI** models. It helps troubleshoot AWS incidents and answers general IT questions.
Elasticsearch provides hybrid retrieval; SQLite retains preferences, product interactions,
retrieved evidence, incidents, and conversation history.

Open **http://127.0.0.1:8000/**. API documentation: **http://127.0.0.1:8000/docs**.

## What is included

- A simple conversation interface with a searchable **100-product AWS catalog**, optional
  product selection, source links, conversation history, cancellation, and mobile support.
- Official AWS documentation scraped into **394 pages / 2,674 passages**, covering 100
  products. These are selected overview and troubleshooting pages, not complete manuals.
  The scientist corpus has been removed from both Elasticsearch and project snapshots.
- One Google ADK orchestrator with knowledge, AWS support, and memory specialists, exposed
  through `AgentTool`. Generation uses `openai/gpt-4.1-mini`; embeddings use the OpenAI SDK
  with `text-embedding-3-small` and 768 dimensions. No Gemini calls or Google key are needed.
- Automatic local hybrid retrieval for selected/recognized AWS product questions, cached
  once per turn; specialist searches can refine it. BM25 and vector searches run concurrently,
  then Python reciprocal-rank fusion combines them without a paid Elasticsearch RRF feature.
- Official **AWS Knowledge MCP** for live documentation and regional availability, plus a
  local **read-only AWS diagnostics MCP** for named CloudWatch alarms, EC2, RDS, and Lambda.
- Durable user-scoped SQLite preferences and a normalized product interaction schema.

## Run

The current workspace already has an ignored `.env`, a Python environment at
`../../.venv`, and Elasticsearch running at `http://127.0.0.1:9200`. Keep the existing `.env`.
From this project directory:

```powershell
..\..\.venv\Scripts\python.exe -m uvicorn chatbot.api:app --host 127.0.0.1 --port 8000
```

For a fresh installation, use Python 3.11+ and an Elasticsearch 8.x instance:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
# Only on a fresh installation:
Copy-Item .env.example .env
# Edit .env: set OPENAI_API_KEY and Elasticsearch connection settings.
# If Docker is installed: docker compose up -d --wait
python scripts/restore_aws.py
python -m uvicorn chatbot.api:app --host 127.0.0.1 --port 8000
```

The archive includes `frontend/dist`. To rebuild it, run `npm ci` and `npm run build` in
`frontend/`, then restart FastAPI. For frontend development use `npm run dev`; Vite proxies
`/api` to port 8000. Run one Uvicorn worker: concurrency reservations are process-local.

## Import and refresh AWS documentation

```powershell
# Scrape official docs; resume products already fully indexed:
python scripts/import_aws.py
# Fetch the selected pages again and replace their chunks:
python scripts/import_aws.py --refresh
# Restore all packaged snapshots without scraping (still calls OpenAI embeddings):
python scripts/restore_aws.py
```

`import_aws.py` discovers guides and troubleshooting pages from the official documentation
catalog and guide tables of contents. It accepts only HTTPS `docs.aws.amazon.com` pages,
handles redirects, strips navigation, and bounds concurrency to three products. Each article
is capped at 60,000 characters; truncation is recorded. Previously imported pages are retained
when selection expands. The manifest records source URLs, timestamps, hashes, product IDs,
and chunk counts. Resume checks each product's indexed chunk count; `--refresh` fetches anew.

The active index is `pingo-products-openai-v1`. Model/dimension metadata prevents accidental
comparison across incompatible embeddings. Reuse stable source IDs to replace pages and remove
obsolete tail chunks. `restore_aws.py` verifies all snapshot hashes before embedding anything.
OpenAI generation and embedding calls require network access and incur usage charges.

## Memory and product interactions

See [the schema and SQL examples](docs/memory.md). The key distinction is:

- **selected**: the user selected a product in the UI;
- **mentioned**: the user's text explicitly matched a product name/alias;
- **retrieved**: that product appeared in local RAG results, which does not establish usage.

Each turn has a server-generated interaction ID, user ID, conversation ID, original question,
status, and timestamps. Retrieved source/chunk/rank/score rows link back to that turn. Incident
records track open/resolved state, user evidence, and confirmed resolutions when the memory
specialist records them. Suggestions must not be stored as confirmed fixes.

Pingo now automatically extracts useful technical context after each completed reply:
application stacks, versions, deployment tools/regions, constraints, product usage, learning
interests, recurring symptoms, and confirmed fixes. It saves evidence-backed facts in the
**`memories` table** without requiring a “remember this” request. A question about a product
is recorded as interest, not assumed ownership. App-specific facts stay scoped to that app.

Extraction runs in a durable background queue after the answer, so it does not delay chat.
It uses one bounded structured-output OpenAI call per completed turn, with up to three attempts
on failure. Pending jobs survive restarts. `GET /api/memories/status` shows queue status.
Set `AUTO_MEMORY_ENABLED=false` to stop extraction; `MEMORY_EXTRACTION_MODEL` defaults to
`gpt-4.1-mini`. This feature starts with new completed replies, not historical backfill.

You can still ask Pingo to remember/correct/forget a preference, record an incident, or recall
product history. Learning means saving and recalling data, not changing model weights. Inspect history
at `/api/product-history?product_id=s3`; preferences are at `/api/memories`. Deleting a chat
also removes its interaction/retrieval/incident rows and automatic facts derived from them;
independently saved explicit preferences remain.

This is a **localhost, single-user sample**: `WEB_USER_ID` selects the backend identity.
Client requests cannot choose a user. Add real authentication and map authenticated principals
to backend identities before using it with multiple customers. Databases live in `data/` and
are excluded from the downloadable archive.

## AWS tools

The [official AWS Knowledge MCP](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
is connected at `https://knowledge-mcp.global.api.aws` using Streamable HTTP. It needs no AWS
account credentials. The support specialist can search/read AWS documentation, list regions,
and check regional product availability. It is subject to the public service's rate limits.

Account diagnostics are installed but **disabled by default**. They require a local AWS
credential profile with read-only permissions. Configure `.env`:

```dotenv
AWS_DIAGNOSTICS_ENABLED=true
AWS_PROFILE=pingo-readonly
AWS_REGION=us-east-1
```

Restart the API after configuration. The local stdio MCP exposes `diagnostics_status` and
`inspect_aws_resource` with four fixed operations:

| Operation | Required IAM action | Input |
| --- | --- | --- |
| `ec2_status` | `ec2:DescribeInstanceStatus` | Instance ID + region |
| `cloudwatch_alarms` | `cloudwatch:DescribeAlarms` | Exact alarm name + region |
| `rds_status` | `rds:DescribeDBInstances` | DB instance ID + region |
| `lambda_status` | `lambda:GetFunctionConfiguration` | Function name + region |

No arbitrary AWS CLI execution or mutating APIs are exposed. Lambda environment variables
are excluded from responses. The assistant must not claim account inspection when diagnostics
are disabled or a request fails. The official [CloudWatch MCP](https://awslabs.github.io/mcp/servers/cloudwatch-mcp-server)
is a possible future extension for deeper logs/metrics analysis; it is not enabled here.

## Project map

| Path | Responsibility |
| --- | --- |
| `src/chatbot/api.py` | FastAPI, conversations, SSE, request identity, turn lifecycle |
| `src/chatbot/agents.py` | ADK orchestrator, specialists, retrieval grounding, MCP wiring |
| `src/chatbot/retrieval.py` | Embeddings, vector index, BM25 + kNN and rank fusion |
| `src/chatbot/memory.py` | SQLite schema, preferences, product and incident records |
| `src/chatbot/learning.py` | Durable extraction worker, structured facts, redaction and validation |
| `src/chatbot/products.py` | Product catalog, matching, isolated per-turn context |
| `src/chatbot/aws_tools_server.py` | Allowlisted read-only AWS diagnostics MCP |
| `src/chatbot/runtime.py` | Shared pooled clients and lifecycle |
| `frontend/src/` | React conversation UI and streaming API client |
| `knowledge/aws/` | 100-product catalog, article snapshots, provenance manifest |
| `scripts/` | Scraper, snapshot restore, local Elasticsearch launcher |
| `tests/`, `frontend/tests/` | Backend, real Elasticsearch and browser checks |

Performance choices: pooled async clients, concurrent hybrid branches, embedding batches of
32, deterministic chunk IDs, SQLite WAL/indexes, per-turn retrieval caching, three concurrent
web turns maximum, one active turn per conversation, and bounded model/tool timeouts. MCP
connections are created only when the support specialist needs them and closed after the turn.

## Verify

```powershell
$env:TEST_ELASTICSEARCH_URL='http://localhost:9200'
python -m pytest -q
ruff check src tests scripts
cd frontend
npm run build
npm test
```

The Elasticsearch integration test uses a disposable test index. Browser tests mock model/API
responses; separate live smoke checks verified embeddings, retrieval, actual OpenAI chat, the
public AWS Knowledge MCP, the local diagnostics MCP, and persisted product evidence.

## Public Azure and Microsoft documentation

Pingo connects to Microsoft's public Learn MCP server at `https://learn.microsoft.com/api/mcp`
through Google ADK's Streamable HTTP MCP client. A dedicated `azure_knowledge_agent` discovers
and uses `microsoft_docs_search`, `microsoft_docs_fetch`, and `microsoft_code_sample_search`.
Ask Azure questions directly in chat; the orchestrator delegates and returns Microsoft source links.
No Azure credentials or local Azure MCP installation are required. Requests query public content,
not private Azure AI Search knowledge bases or resources in a customer's subscription.

The existing product picker and Elasticsearch corpus remain AWS-only. Azure documentation is
queried live; it is not imported into the local index. Catalog-backed incident records still require
an existing catalog product; do not map Azure issues to an unrelated AWS product. General technical
memory extraction can retain Azure application context with a null catalog product ID.

Reference: https://learn.microsoft.com/en-us/training/support/mcp-developer-reference

## ADK long-term memory service

Facts and key takeaways now use a custom durable Google ADK `BaseMemoryService`, shared by
web and CLI runners. Explicit memory tools write `MemoryEntry` objects; automatic extraction
keeps selecting evidence-backed facts after successful replies. ADK `load_memory` and prompt
recall search a dedicated Elasticsearch memory index with SQLite validation/fallback.
See [memory implementation and limits](docs/memory.md#google-adk-long-term-memory).
