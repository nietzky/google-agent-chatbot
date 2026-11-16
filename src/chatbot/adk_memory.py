"""ADK long-term memory: SQLite authority with an eventually consistent search projection."""

import asyncio
import hashlib
import json
import logging

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from chatbot.memory import MemoryStore, select_memory_context
from chatbot.products import current_turn

logger = logging.getLogger(__name__)


class CloudHelpMemoryService(BaseMemoryService):
    """Persist selected facts across ADK sessions; never index raw conversation transcripts."""

    def __init__(self, store, app_name, es=None, index="cloudhelp-memory-v1"):
        # Identity is supplied by ADK/server context. This service owns only its worker connection.
        """Configure the adapter without opening connections or starting background tasks.

        Args:
            store: Initialized MemoryStore used for authoritative reads and explicit writes.
                Its connection is owned by the caller, not by this service.
            app_name: The single ADK application associated with this SQLite database.
            es: Shared AsyncElasticsearch client, or None for SQLite-only recall.
            index: Dedicated memory index; must not be the product-documentation index.

        The runtime attaches this instance to MemoryStore.adk_service and passes it to
        ADK Runner(memory_service=...). worker_store and task are populated by start().
        This constructor does not authenticate users or provision a cloud memory bank.
        """
        self.store, self.app_name, self.es, self.index = store, app_name, es, index
        self.worker_store = None
        self.task = None

    def check_app(self, app_name):
        # A database belongs to one configured application; reject accidental cross-app access.
        """Reject requests for a different ADK application before reading or writing facts.

        Args:
            app_name: Application identity supplied by the ADK runner or trusted caller.

        Returns:
            None when the name matches this service's configured application.

        Raises:
            ValueError: If the application name differs.

        SQLite facts have user scope but no app_name column. Consequently, this guard
        assumes one application per database; it is not a substitute for authentication
        or customer/environment permissions.
        """
        if app_name != self.app_name:
            raise ValueError("Memory application scope mismatch")

    async def start(self, path):
        # A dedicated connection prevents the sync worker from interleaving extraction transactions.
        """Open the worker connection and schedule asynchronous Elasticsearch synchronization.

        Args:
            path: Path to the SAME SQLite database used by the authoritative store.

        Returns:
            None. Scheduling the task does not mean indexing or backfill has finished.

        With es=None this is a no-op. Otherwise MemoryStore.open initializes/migrates
        schema and opens a separate connection, preventing worker statements from
        joining another connection's extraction transaction. Database errors propagate;
        network initialization happens inside run(), where failures are retried.
        The runtime calls this once per instance; repeated calls are not guarded and
        would start additional tasks. Pair the call with close() during shutdown.
        """
        if self.es is not None:
            self.worker_store = await MemoryStore.open(path)
            self.task = asyncio.create_task(self.run(), name="cloud-help-memory-search-sync")

    async def close(self):
        # Stop pending network work before closing our connection; callers own Elasticsearch.
        """Cancel synchronization, await its termination, and close its SQLite connection.

        Returns:
            None after owned resources have been released.

        Cancellation is gathered with return_exceptions=True so an interrupted worker
        does not prevent connection cleanup. Unacknowledged outbox jobs remain durable
        for the next startup. The caller retains ownership of the primary store and
        Elasticsearch client. A worker-connection close error can still propagate.
        """
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.worker_store:
            await self.worker_store.close()

    async def add_session_to_memory(self, session):
        # Queue only the latest completed, server-recorded interaction. Existing trigger/jobs make
        # this idempotent, and the existing extractor selects facts with exact user evidence.
        """Request selected-fact extraction for a session's latest completed interaction.

        Args:
            session: ADK Session whose app_name, user_id and id identify the interaction.
                Its events are not copied into the memory index by this method.

        Returns:
            None after the queue statement and optional worker wake-up signal.

        Only a completed interaction already recorded in SQLite is eligible, and only
        while memory_learning_state.enabled is true. No matching row means no insertion.
        INSERT OR IGNORE and the job's interaction-ID primary key make repeated calls
        idempotent, including calls after the completion trigger has already queued it.
        A previously done/failed job is not reset. Earlier interactions are not backfilled.

        The separate AutomaticMemory worker extracts and validates evidence-backed
        facts; this call does not wait for extraction or invoke a model itself. An attached
        learning worker is signaled if present, even if no job was inserted. Application
        scope and database errors propagate to the caller.
        """
        self.check_app(session.app_name)
        await self.store.db.execute(
            "INSERT OR IGNORE INTO memory_jobs(interaction_id) "
            "SELECT interaction_id FROM interactions WHERE user_id=? AND session_id=? "
            "AND status='completed' AND EXISTS(SELECT 1 FROM memory_learning_state "
            "WHERE id=1 AND enabled=1) ORDER BY created_at DESC LIMIT 1",
            (session.user_id, session.id),
        )
        if self.store.learning:
            self.store.learning.wake.set()

    async def add_memory(self, *, app_name, user_id, memories, custom_metadata=None):
        # Explicit writes use ADK MemoryEntry with a stable key and supporting quote in metadata.
        # Preserve the existing manual-memory validation, forgetting and consolidation behavior.
        """Persist explicit facts supplied as ADK MemoryEntry objects.

        Args:
            app_name: Must match this service's configured application.
            user_id: Trusted server/ADK identity that owns every entry in this call.
            memories: Iterable of MemoryEntry objects. Each entry.id is the stable key;
                text parts are concatenated without a separator into the saved value.
                entry.custom_metadata supplies kind (default 'takeaway') and evidence.
            custom_metadata: Accepted for ADK interface compatibility; currently ignored.
                Metadata must be supplied on each entry instead.

        Returns:
            None. Successful entries are available in SQLite before this call returns.

        Delegates validation and upsert to MemoryStore.save: kind must be preference or
        takeaway, keys are nonempty and at most 100 characters, and value/evidence are
        nonempty and at most 1,000 characters. Existing user/key facts are replaced as
        manual memories with no expiry; their forgetting marker is cleared. The outbox
        trigger schedules search indexing, which this method does not await.

        Entry timestamp/author and other custom metadata are not persisted by this path.
        The method does not verify semantic truth or run automatic extraction/redaction.
        Writes are sequential, not a batch transaction: if an entry fails validation or
        storage, earlier entries remain saved. Exceptions propagate.
        """
        self.check_app(app_name)
        for entry in memories:
            metadata = entry.custom_metadata
            value = "".join(part.text or "" for part in entry.content.parts or [])
            await self.store.save(
                user_id,
                entry.id or "",
                value,
                metadata.get("kind", "takeaway"),
                metadata.get("evidence", ""),
            )

    async def search_memory(self, *, app_name, user_id, query):
        # Filter ownership before relevance. Validate every search hit against current SQLite facts
        # so deleted, expired or corrected copies can never be recalled from a stale search index.
        """Return a bounded, user-scoped set of current facts in ADK's response format.

        Args:
            app_name: Must match the service's single application scope.
            user_id: Trusted owner identity, normally supplied by ADK ToolContext.
            query: Natural-language search text. Only the first 4,000 characters are
                sent to Elasticsearch; an empty query skips Elasticsearch.

        Returns:
            SearchMemoryResponse containing at most 12 MemoryEntry objects. Each entry
            has its stable key as id, author='user', updated_at as timestamp, and JSON
            text plus custom_metadata containing value, evidence, subject, provenance,
            original source time and expiry. The JSON text budget is 6,500 characters;
            it is not a token limit or a bound on the entire serialized ADK response.

        First read unexpired SQLite facts for this user. If the current turn belongs to
        that user and has a selected product, restrict to that product plus unassigned
        facts. Without a selection, all of the user's products are eligible. Subject
        labels convey application/environment context but are not authorization filters.

        Then request up to 30 BM25 candidates, filtering app/user/product before ranking.
        Value and subject have boosts of 3 and 2. The search call has a two-second budget.
        Only hits whose key, value and updated_at match the SQLite snapshot are accepted;
        returned content always comes from SQLite, never an unchecked search document.

        Append nonduplicate SQLite fallback candidates, even after successful search,
        to admit new facts before indexing. Elasticsearch errors fall back to SQLite;
        SQLite/scope errors propagate. Skip entries exceeding the remaining text budget.
        This is snapshot consistency: a concurrent correction after the initial SQLite
        read may be reflected on the next call. Reading all eligible rows is suitable
        for this sample but requires pagination/scaling work for very large user histories.
        """
        self.check_app(app_name)
        turn = current_turn.get()
        product = turn.product_id if turn and turn.user_id == user_id else None
        rows = await (
            await self.store.db.execute(
                "SELECT * FROM memories WHERE user_id=? AND "
                "(expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "AND (? IS NULL OR product_id IS NULL OR product_id=?) ORDER BY updated_at DESC",
                (user_id, product, product),
            )
        ).fetchall()
        current = {row["key"]: dict(row) for row in rows}
        ranked = []
        if self.es is not None and query.strip():
            filters = [{"term": {"app_name": app_name}}, {"term": {"user_id": user_id}}]
            if product:
                filters.append(
                    {
                        "bool": {
                            "should": [
                                {"term": {"product_id": product}},
                                {"bool": {"must_not": {"exists": {"field": "product_id"}}}},
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                )
            try:
                async with asyncio.timeout(2):
                    result = await self.es.search(
                        index=self.index,
                        size=30,
                        query={
                            "bool": {
                                "filter": filters,
                                "must": {
                                    "multi_match": {
                                        "query": query[:4000],
                                        "fields": ["value^3", "subject^2", "attribute", "category"],
                                    }
                                },
                            }
                        },
                    )
                for hit in result["hits"]["hits"]:
                    source = hit["_source"]
                    row = current.get(source["key"])
                    if (
                        row
                        and row["value"] == source["value"]
                        and row["updated_at"] == source["updated_at"]
                    ):
                        ranked.append(row)
            except Exception:
                logger.info("Memory search unavailable; using authoritative SQLite recall")
        # Include fresh writes immediately, even before search synchronization, within a strict budget.
        seen = {row["key"] for row in ranked}
        ranked.extend(
            row
            for row in select_memory_context(list(current.values()), query, product)
            if row["key"] not in seen
        )
        entries, budget = [], 0
        for row in ranked:
            payload = {
                k: row[k]
                for k in (
                    "key",
                    "value",
                    "evidence",
                    "category",
                    "subject",
                    "attribute",
                    "product_id",
                    "origin",
                    "source_interaction_id",
                    "updated_at",
                    "source_at",
                    "expires_at",
                )
            }
            text = json.dumps(payload)
            if budget + len(text) > 6500:
                continue
            entries.append(
                MemoryEntry(
                    id=row["key"],
                    author="user",
                    timestamp=row["updated_at"],
                    content=types.Content(parts=[types.Part(text=text)]),
                    custom_metadata=payload,
                )
            )
            budget += len(text)
            if len(entries) == 12:
                break
        return SearchMemoryResponse(memories=entries)

    async def sync_one(self):
        # The durable outbox collapses repeated writes per fact. Delete by job ID only, so a
        # correction arriving during indexing retains its newer job for the next pass.
        """Apply the oldest queued fact change to Elasticsearch and acknowledge that job.

        Returns:
            False if the outbox is empty; True after one job is successfully processed.

        Uses the dedicated worker connection, or the main connection for focused tests.
        The current SQLite row is authoritative: index it if present and unexpired,
        otherwise delete its search document (a missing document/HTTP 404 is harmless).
        Document IDs are deterministic SHA-256 hashes of [app_name, user_id, key], so
        retries target the same document without duplicating it. No embeddings are made.

        Acknowledge by the captured job ID only. Triggers replace a queued user/key job
        with a new ID on another write; a correction arriving during network I/O therefore
        retains its newer job. Network/database errors propagate, leaving unfinished work
        for retry. A crash after indexing but before acknowledgment safely repeats work.

        Requires an Elasticsearch client and initialized index. Does not force an index
        refresh, so successful processing need not be immediately searchable. This is a
        single-worker design, not a leased distributed queue. A failing oldest job can
        block later jobs until the underlying error is corrected.
        """
        db = (self.worker_store or self.store).db
        job = await (
            await db.execute("SELECT * FROM memory_search_jobs ORDER BY id LIMIT 1")
        ).fetchone()
        if job is None:
            return False
        row = await (
            await db.execute(
                "SELECT * FROM memories WHERE user_id=? AND key=? AND "
                "(expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (job["user_id"], job["key"]),
            )
        ).fetchone()
        identifier = hashlib.sha256(
            json.dumps([self.app_name, job["user_id"], job["key"]]).encode()
        ).hexdigest()
        if row:
            doc = dict(row)
            doc["app_name"] = self.app_name
            await self.es.index(index=self.index, id=identifier, document=doc)
        else:
            await self.es.options(ignore_status=404).delete(index=self.index, id=identifier)
        await db.execute("DELETE FROM memory_search_jobs WHERE id=?", (job["id"],))
        return True

    async def run(self):
        # Retry transient search failures without failing chat. Backfill selected facts, never raw
        # sessions. Periodically prune expired projection documents as well as filtering on recall.
        """Maintain the dedicated search index and drain the durable outbox until cancelled.

        Returns:
            Never during normal operation; cancellation terminates the coroutine.

        On its first successful initialization, create mappings if the index is absent
        and enqueue all existing selected facts with INSERT OR IGNORE. Existing mappings
        are not checked or migrated. Backfill does not regenerate facts from old chats.
        Expired rows may enter the queue and will be deleted by sync_one rather than indexed.

        Process one job per iteration and continue immediately while work is available.
        When idle or after errors, sleep two seconds. Errors log only their class name;
        they do not kill the chat service. Unlike extraction jobs, synchronization has no
        maximum retry count, per-job retry counter, or terminal failed state.

        Prune expired documents for this app on pass zero and every 300 passes. This is
        an iteration interval, not a fixed schedule: roughly ten minutes while idle,
        faster when draining jobs, and potentially longer during network delays. Retrying
        initialization or pruning can delay queue processing. start() must provide the
        worker connection; close() cancels this loop before dependencies are closed.
        """
        ready, passes = False, 0
        while True:
            try:
                if not ready:
                    if not await self.es.indices.exists(index=self.index):
                        await self.es.indices.create(
                            index=self.index,
                            mappings={
                                "properties": {
                                    **{
                                        k: {"type": "keyword"}
                                        for k in ("app_name", "user_id", "key", "product_id")
                                    },
                                    "value": {"type": "text"},
                                    "subject": {"type": "text"},
                                    "attribute": {"type": "text"},
                                    "category": {"type": "text"},
                                    "expires_at": {"type": "date"},
                                    "updated_at": {"type": "date"},
                                }
                            },
                        )
                    await self.worker_store.db.execute(
                        "INSERT OR IGNORE INTO memory_search_jobs(user_id,key) SELECT user_id,key FROM memories"
                    )
                    ready = True
                if passes % 300 == 0:
                    await self.es.delete_by_query(
                        index=self.index,
                        conflicts="proceed",
                        query={
                            "bool": {
                                "filter": [
                                    {"term": {"app_name": self.app_name}},
                                    {"range": {"expires_at": {"lte": "now"}}},
                                ]
                            }
                        },
                    )
                passes += 1
                if await self.sync_one():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Memory index sync deferred: %s", type(exc).__name__)
            await asyncio.sleep(2)
