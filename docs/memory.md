# Cloud Help memory schema

`data/memory.sqlite3` stores long-term records. ADK event history is separately managed in
`data/sessions.sqlite3`. Existing preference rows are preserved by additive, idempotent schema
creation. SQLite uses WAL, foreign keys, a busy timeout, and indexes for user/session/product
lookups. All queries use bound parameters.

| Table | Key / links | Purpose |
| --- | --- | --- |
| `memories` | `(user_id, key)` | Explicit and automatically extracted preferences/technical takeaways, with supporting user quote |
| `products` | `product_id` | Stable AWS product ID, name and documentation URL |
| `interactions` | `interaction_id`; user/session | One request with question, running/completed/failed/cancelled status and timestamps |
| `interaction_products` | `(interaction_id, product_id, relationship)` | Selected, mentioned or retrieved product relationship |
| `retrievals` | `retrieval_id`; interaction/product FKs | Query, search mode, source, chunk, rank, fused score and retrieval time |
| `memory_jobs` | interaction FK | Durable extraction state, attempts, lease/retry time, saved count and sanitized error code |
| `memory_forgetting` | user/key | Suppression records preventing forgotten facts from being relearned from queued messages |
| `memory_learning_state` | singleton | Whether completed replies should enqueue new jobs |
| `incidents` | `incident_id`; unique user/session/product | User-reported summary, open/resolved status, resolution and user evidence |

A selected product limits both BM25 and kNN results to that product. With no selection the
search spans the corpus, and results may include several products. Product matching is based
on catalog names and aliases; it is evidence of a text mention, not proof of ownership or usage.
Cross-product support sessions can have multiple relationship and incident rows.

Every web/CLI request creates an interaction. The server supplies identities; the LLM cannot
choose them. RAG evidence links to the same interaction through a task-local ContextVar, so
concurrent turns do not share context. Preferences are read on each turn. Incident updates are
performed by the memory specialist; automatic interaction/retrieval recording is independent
of whether the model chooses to record an incident. Abrupt process termination can leave an
interaction marked `running`; normal failures and disconnects are recorded separately.

Examples (use your actual backend user ID, default `local-user`):

```sql
-- Products this user selected/mentioned; excludes incidental search results.
SELECT p.name, ip.relationship, count(DISTINCT i.interaction_id) AS turns,
       max(i.created_at) AS last_interaction
FROM interactions i
JOIN interaction_products ip USING(interaction_id)
JOIN products p USING(product_id)
WHERE i.user_id = 'local-user' AND ip.relationship IN ('selected','mentioned')
GROUP BY p.product_id, ip.relationship ORDER BY last_interaction DESC;

-- Which sources grounded S3 conversations?
SELECT i.session_id, i.question, r.query, r.source, r.chunk, r.rank, r.score
FROM interactions i JOIN retrievals r USING(interaction_id)
WHERE i.user_id = 'local-user' AND r.product_id = 's3'
ORDER BY i.created_at DESC, r.retrieval_id;

-- Open incidents versus confirmed fixes.
SELECT product_id, summary, status, resolution, evidence, updated_at
FROM incidents WHERE user_id = 'local-user' ORDER BY updated_at DESC;
```

`GET /api/product-history?product_id=s3` returns current-user interaction relationships and
incident records, newest first (up to 100 rows in each collection). `GET /api/memories` lists
preferences; `DELETE /api/memories` accepts `{"key":"response_style"}`. Conversation deletion
cascades to retrieval and product relationships and deletes its incident records. Preferences
are independently managed and retained until explicitly forgotten.

The app intentionally has no analytics dashboard: use these APIs or SQL for inspection while
keeping the support UI focused on conversation. Back up both SQLite databases consistently
when the API is stopped. Do not ship local databases or credentials to other users.


## Automatic technical memory

After a successful web or CLI reply, a SQLite trigger queues one extraction job in the same
statement that marks the interaction complete. Failed/cancelled turns do not enqueue jobs.
The UI receives its answer without waiting for the model call. A background worker claims
jobs with a 60-second lease; expired leases are recovered after a crash. Each extraction has
a 40-second budget and at most three attempts, with backoff. Exceptions do not fail the chat.

The extractor uses structured OpenAI output with no tools. It receives the current user
message, up to six recent public messages, existing memories and product IDs. Common credential
formats are redacted before this additional model call. Candidates require a verbatim quote
from the current user message, confidence >=0.85, valid category/product ID, and length limits.
Candidate values/evidence are checked again for common secrets. These filters reduce risk but
cannot identify every possible secret; users should never paste credentials into chat.

Categories: `preference`, `product_usage`, `interest`, `environment`, `application`, `constraint`,
`symptom`, `confirmed_fix`. Assistant suggestions alone never establish a successful fix.
A user's product question supports learning interest, not an assertion they use that product.

The original `memories` table is extended additively with `category`, `subject`, `attribute`,
`product_id`, `origin`, `source_interaction_id`, `source_at`, and `expires_at`. Origin is manual
or automatic. Subjects distinguish applications/environments from user-wide preferences.
For example `application:billing-api:runtime` can store Python 3.13 with the user's correction
as evidence. The extractor reuses an existing fact's key; same-subject/attribute automatic
facts are merged, and older jobs cannot overwrite newer facts. Semantic interpretation still
uses a model: inspect/correct a fact through the existing memory tools if it misunderstood.

Automatic memory expiry: symptoms 14 days, interests 90, stack/environment/application/constraint
facts 180, preferences and confirmed fixes 365. Expired rows remain inspectable in SQL but are
excluded from `/api/memories` and prompt recall. Explicitly saved memories do not expire.
The orchestrator selects at most 12 relevant memories within a 6,500-character profile budget,
favoring product/application relevance and preferences. It is instructed to respect scope,
uncertainty and current user corrections.

Forgetting a key suppresses future automatic re-creation of that key; an explicit save can
re-enable it. All source messages older than a user's forget operation are conservatively
blocked from writing new automatic facts, including in-flight jobs. Deleting a conversation
atomically removes automatic facts whose current source interaction belongs to it. This
includes their pending jobs; user-authored explicit preferences remain independently managed.

Configuration (restart the API after changing):

```dotenv
AUTO_MEMORY_ENABLED=true
MEMORY_EXTRACTION_MODEL=gpt-4.1-mini
```

`GET /api/memories/status` returns enabled state and per-user job counts (`queued`, `running`,
`done`, `failed`). Extraction starts on newly completed replies; historical chat backfill is
not automatic. An empty extraction is valid for a greeting or a message with no useful facts.

```sql
SELECT key, category, subject, attribute, value, evidence, product_id,
       origin, source_interaction_id, source_at, expires_at
FROM memories WHERE user_id = 'local-user' ORDER BY updated_at DESC;

SELECT j.status, j.attempts, j.saved_count, j.error_code, i.session_id
FROM memory_jobs j JOIN interactions i USING(interaction_id)
WHERE i.user_id = 'local-user' ORDER BY i.created_at DESC;
```

Structured extraction follows the [OpenAI Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

## Incident resolution feedback

After a tool-backed answer for an open incident, Cloud Help appends a conversational check asking
whether the answer solved the issue, helped partially, or left it unresolved. The question is
saved with the assistant message, so it remains visible when reopening the conversation.
Users can describe the steps they took and which guidance helped. The memory specialist
records confirmed success as `resolved`; partial progress stays `open`.

`GET /api/product-history` now includes these incident fields:

- `assistance`: observed successful RAG/MCP calls, with tool name, source type, interaction ID,
  and retrieval mode for RAG. Failed calls and diagnostics availability checks are excluded.
- `helpful_source`: `rag`, `mcp`, `both`, `neither`, or `unknown`, based on user feedback.
- `contribution`: `solved`, `helped`, `not_helpful`, or `unknown`.
- `resolution_sources`: recorded sources attributed to a confirmed resolution. This is empty
  when attribution is unknown, even if tools were used. An observed source is not proof it helped.

Resolution evidence must quote the current user message. Interpreting success, partial help,
and source attribution remains an agent judgment; this is not an independent AWS recovery check.
Existing incidents migrate without invented historical attribution. The existing one-incident
per user/conversation/product constraint remains. No external AWS Support ticket is created.

## Google ADK long-term memory

Cloud Help now implements `BaseMemoryService` in `src/chatbot/adk_memory.py` and passes the
same durable service to both HTTP and CLI ADK runners. The orchestrator uses ADK's
`load_memory` tool for additional recall. Bounded prompt recall also uses this service.

- `add_memory`: explicit facts/key takeaways are saved as ADK `MemoryEntry` objects with
  a stable ID, kind and supporting evidence. SQLite remains the authoritative store.
- `add_session_to_memory`: idempotently queues the latest completed server-recorded turn
  for the existing evidence-based extraction worker. It does not index raw session text.
  The existing completion trigger remains a crash-safe queueing mechanism.
- `search_memory`: retrieves through the separate `cloudhelp-memory-v1` Elasticsearch collection,
  filters by application/user and selected product, validates against current SQLite facts,
  and returns at most 12 entries within a 6,500-character content budget, including evidence,
  provenance, subject and age. This collection uses BM25 keyword relevance, not embeddings;
  the separate AWS documentation RAG still uses hybrid keyword/vector retrieval.

Every fact insertion, correction or deletion writes a durable SQLite outbox job in the same
transaction. A worker retries projection updates every two seconds while idle or after errors.
Existing selected facts are backfilled on startup. Failed indexing does not undo fact writes.
Immediate recall works through SQLite while synchronization is pending or Elasticsearch is down.
Deleted/expired/corrected search hits are checked against SQLite before use; forgotten facts cannot
be recalled merely because an older Elasticsearch copy remains. Expired projection records are
periodically pruned. Corrections replace a stable fact slot; this does not add a supersession audit
trail. No model retraining occurs. No Google Cloud account is required for this custom ADK service.

`/api/memories/status` now includes `memory_service`, `search_index`, and
`pending_index_updates` for the configured user. Change `MEMORY_INDEX` to use another dedicated
search index; never reuse the product documentation index. Changing the index name requires separate
cleanup of the retired index. Only the currently configured index is synchronized.

Scope limitation: this remains the local single-user sample. Application names are fact subjects,
not authenticated customer/environment ACLs. Customer/environment authorization must be added before
multi-tenant deployment; the model must never choose an identity or permission scope. Raw conversation
logs stay in the session database. User-confirmed fixes are observations, not reviewed procedures;
this change does not create a procedure approval workflow or promote memories into shared runbooks.

ADK reference: https://github.com/google/adk-docs/blob/main/docs/sessions/memory.md

For method contracts, examples, queue semantics, consistency guarantees and troubleshooting,
see the [ADK memory service developer guide](adk-memory-service.md).
