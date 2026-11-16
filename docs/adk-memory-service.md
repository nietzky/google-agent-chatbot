# Cloud Help ADK memory service: developer guide

Implementation: [`src/chatbot/adk_memory.py`](../src/chatbot/adk_memory.py).
Every method includes a detailed docstring describing its contract and limits.

## Purpose and ownership

`CloudHelpMemoryService` implements Google ADK's `BaseMemoryService`. It lets ADK save
explicit facts and recall useful information across conversations while retaining
Cloud Help's existing evidence-based background extraction.

SQLite is authoritative. Elasticsearch is a replaceable, asynchronously updated
search copy. The service does not train a model, store an entire transcript as a
fact, or use Google's managed Memory Bank. Chat and extraction still use the
configured OpenAI models; the search projection uses no model calls.

| Component | Responsibility |
| --- | --- |
| `runtime.resources()` | Opens dependencies, attaches the service to `memory.adk_service`, starts synchronization, and closes resources in order. |
| HTTP and CLI `Runner` | Receives `memory_service=...`, allowing ADK tool context to search memory. |
| `agents.save_memory` | Wraps an explicit preference/takeaway in a `MemoryEntry` and calls `add_memory`. |
| `agents.instruction` | Loads bounded relevant facts into the initial orchestrator instructions. |
| ADK `load_memory` | Lets the orchestrator request additional recall through the configured service. |
| `AutomaticMemory` in `learning.py` | Extracts candidate facts, verifies evidence, consolidates corrections and sets expiration. |
| `MemoryStore` in `memory.py` | Owns schema, authoritative writes, forgetting and transactionally queued indexing work. |
| `CloudHelpMemoryService.run` | Maintains the Elasticsearch projection from the durable outbox. |

The caller owns the primary SQLite store and Elasticsearch client. The service
owns its synchronization task and an additional SQLite connection to the same file.
Call `start()` once per instance and pair it with `close()`. Starting the worker is
not a readiness guarantee. The web application remains usable if indexing is down.

## Function reference

| Method | Inputs | Result / side effect |
| --- | --- | --- |
| `__init__(store, app_name, es, index)` | Initialized store, trusted app scope, optional ES client, dedicated index name | Stores dependencies. No network or database calls. |
| `check_app(app_name)` | Requested ADK app name | Returns `None` or raises `ValueError` on mismatch. |
| `start(path)` | Same database path as the primary store | Opens the worker connection and schedules `run`; no-op without ES. |
| `close()` | None | Cancels/awaits the task and closes its connection. Shared dependencies remain open. |
| `add_session_to_memory(session)` | ADK session app/user/session identity | Idempotently queues its latest completed interaction when learning is enabled. Does not consume `session.events`. |
| `add_memory(...)` | App/user identity and entries with stable IDs, text, kind and evidence | Sequentially upserts explicit manual memories through `MemoryStore.save`. Returns `None`. |
| `search_memory(...)` | App/user identity and query | Returns `SearchMemoryResponse` with up to 12 current facts and provenance. |
| `sync_one()` | Oldest outbox job, read internally | Indexes/deletes one document and acknowledges the job; returns whether it processed work. |
| `run()` | Dependencies stored on the instance | Initializes/backfills the index, retries synchronization and periodically prunes expired documents until cancelled. |

`add_events_to_memory` is not overridden. The inherited ADK method raises
`NotImplementedError`; use the supported session or explicit-entry interface.

## Writing an explicit fact

Inside a trusted backend path where the service and identity are already available:

```python
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

await memory.adk_service.add_memory(
    app_name=memory.adk_service.app_name,
    user_id=authenticated_user_id,  # Supplied by the server, never a model argument.
    memories=[MemoryEntry(
        id="example_language",
        content=types.Content(parts=[types.Part(text="Prefers Python examples")]),
        custom_metadata={
            "kind": "preference",
            "evidence": "Please use Python in your examples.",
        },
    )],
)
```

The current sample uses `WEB_USER_ID`; `authenticated_user_id` above illustrates
what a future authenticated caller should provide, not an existing login feature.

Entry IDs become SQLite memory keys. Text parts are concatenated. Per-entry
`kind` defaults to `takeaway`; evidence is required by storage validation. Other
entry metadata, author and timestamp are not copied by this explicit write path.
The top-level `custom_metadata` argument is accepted but ignored.

Explicit memories are manual, have no expiry, and replace the matching user/key.
A save clears that key's forgetting marker. The write list is not atomic: if entry
three fails, entries one and two remain saved. The service does not independently
prove evidence or redact explicit inputs; automatic extraction has separate checks.

## Background capture versus indexing

These are two different queues:

1. `memory_jobs` queues **model extraction**. A successful interaction's completion
   trigger queues it; `add_session_to_memory` provides an idempotent ADK entry point
   for the latest completed interaction. It neither resets completed/failed jobs
   nor starts a historical transcript backfill.
2. `memory_search_jobs` queues **Elasticsearch synchronization**. SQLite triggers
   enqueue inserts, updates and deletes of facts, including automatic extraction,
   manual saves, forgetting and deletion of session-derived memories.

The extraction worker has its existing timeout and retry policy. The indexing
worker retries indefinitely, sleeping two seconds after errors or while idle.
A broken oldest indexing job can block later jobs. It has no dead-letter queue.

On startup, the indexing worker enqueues existing facts. It does not re-extract
facts from old conversations. It creates a missing index but does not validate or
migrate an existing index mapping.

## Retrieval and consistency

1. Verify app scope and load unexpired SQLite facts for the user.
2. If their current turn selects a product, restrict candidates to that product
   and unassigned facts. Otherwise their facts across products are eligible.
3. Search ES with app/user/product filters and BM25 relevance, requesting at most
   30 hits. Search uses `value^3`, `subject^2`, `attribute`, and `category`; the query
   sent to ES is capped at 4,000 characters and the request has a two-second budget.
4. Accept hits only when key, value and update time match the SQLite snapshot.
   Return authoritative SQLite content rather than the search document's content.
5. Append nonduplicate SQLite-selected candidates to admit facts not yet indexed.
6. Return at most 12 entries whose JSON content totals at most 6,500 characters.
   Oversized entries are skipped; the surrounding ADK response also includes metadata,
   so its total serialized size is larger than this content budget.

Elasticsearch errors fall back to SQLite. SQLite errors propagate. Fallback ranking
uses product alignment, preferences and query-term matches; it can return useful
preferences even without a literal search match. It does not prove a fact is relevant.
The service reads all eligible SQLite rows, which needs further scaling work for
large histories. Memory search is BM25; AWS documentation retrieval remains hybrid.

`timestamp` is `updated_at`, not the original occurrence time. `source_at` and
`source_interaction_id` identify provenance when available. Values/evidence remain
untrusted data for the agent. Scope and age still need to be considered in its answer.

A queued user/key change replaces the older job with a new auto-incremented ID.
Acknowledging only the processed ID preserves corrections arriving during indexing.
Document IDs hash app/user/key, so retries are repeatable. Index writes do not force
refresh. A stale search hit cannot override the SQLite snapshot, but a concurrent
change after that snapshot may be visible only on the next recall call.

## Expiration, forgetting and boundaries

Expired facts are excluded immediately by SQLite recall. Deletions enqueue removal
from ES. Periodic ES expiry pruning occurs on pass zero and every 300 worker passes,
not at a fixed wall-clock interval. While idle, that is roughly ten minutes; queued
work can make it faster and network delays can make it slower.

An unavailable ES service can retain a stale physical document until synchronization
recovers. It is excluded from Cloud Help recall, but direct ES administrators can still
see it until removed. Retired indexes are not maintained after changing `MEMORY_INDEX`.

This is one application per SQLite database, with user and optional product filters.
An application/environment subject is descriptive metadata, not an ACL. This service
does not add customer authorization, an application-context selector, version history,
or reviewed diagnostic procedures. Corrections replace current fact slots; user-
confirmed outcomes are not automatically approved shared runbooks.

## Operations and checks

- `/api/memories`: active facts for the configured user.
- `/api/memories/status`: extraction job counts, ADK service name, active search index
  and the configured user's pending indexing count. Zero pending updates is not a
  full index-health check and does not promise that ES refresh has occurred.
- `MEMORY_INDEX`: defaults to `cloudhelp-memory-v1`; runtime rejects using the product index.
- `Memory index sync deferred: <ExceptionType>`: indexing will retry; inspect ES availability
  and mappings without logging credentials or fact contents.
- `Memory search unavailable; using authoritative SQLite recall`: recall used fallback.
- `tests/test_adk_memory.py`: restart persistence, isolation, corrections, stale/expired
  hits, forgetting, retry, queue idempotency, product filtering and real ADK tool dispatch.

For local checks from the project directory:

```powershell
..\..\.venv\Scripts\ruff.exe check src/chatbot/adk_memory.py
..\..\.venv\Scripts\pytest.exe tests/test_adk_memory.py -p no:cacheprovider --basetemp ../../work/pytest-adk-doc-check
```

Use a fresh scratch directory for each pytest run; pytest may clear its `--basetemp`.
