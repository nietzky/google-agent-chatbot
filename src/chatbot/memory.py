"""User-scoped relational memory. Values are data, never agent instructions."""

import json
import re
import uuid

import aiosqlite

from chatbot.products import mentioned_products, products


class MemoryStore:
    # Wrap an existing async SQLite connection and reserve a reference for the optional
    # extraction worker. Schema initialization is handled by open, not this constructor.
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self.learning = None
        self.adk_service = None

    # Create the database directory and connection, enable WAL and foreign keys, and create or
    # additively migrate the memory and product-history schema. Install completion/deletion
    # triggers and synchronize catalog products before returning the store.
    @classmethod
    async def open(cls, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        await db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS memories (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('preference', 'takeaway')),
                evidence TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(user_id, key)
            );
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                docs_url TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS interactions_user_session
                ON interactions(user_id,session_id,created_at);
            CREATE TABLE IF NOT EXISTS interaction_products (
                interaction_id TEXT NOT NULL REFERENCES interactions ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products,
                relationship TEXT NOT NULL CHECK(relationship IN ('selected','mentioned','retrieved')),
                PRIMARY KEY(interaction_id,product_id,relationship)
            );
            CREATE INDEX IF NOT EXISTS interactions_by_product
                ON interaction_products(product_id,interaction_id);
            CREATE TABLE IF NOT EXISTS retrievals (
                retrieval_id INTEGER PRIMARY KEY,
                interaction_id TEXT NOT NULL REFERENCES interactions ON DELETE CASCADE,
                query TEXT NOT NULL,
                mode TEXT NOT NULL,
                source TEXT NOT NULL,
                chunk INTEGER NOT NULL,
                product_id TEXT REFERENCES products,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                retrieved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS retrievals_turn ON retrievals(interaction_id);
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                product_id TEXT NOT NULL REFERENCES products,
                summary TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','resolved')),
                resolution TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(user_id,session_id,product_id)
            );
            CREATE INDEX IF NOT EXISTS incidents_user_product ON incidents(user_id,product_id,status);
        """)
        # Additive migration preserves pre-existing explicit memories.
        # Add feedback metadata without replacing existing incidents.
        incident_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(incidents)")).fetchall()
        }
        for name, definition in {
            "assistance": "TEXT NOT NULL DEFAULT '[]'",
            "resolution_sources": "TEXT NOT NULL DEFAULT '[]'",
            "helpful_source": "TEXT NOT NULL DEFAULT 'unknown'",
            "contribution": "TEXT NOT NULL DEFAULT 'unknown'",
        }.items():
            if name not in incident_columns:
                await db.execute(f"ALTER TABLE incidents ADD COLUMN {name} {definition}")
        columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(memories)")).fetchall()
        }
        for name, definition in {
            "category": "TEXT NOT NULL DEFAULT 'general'",
            "subject": "TEXT NOT NULL DEFAULT 'user'",
            "attribute": "TEXT NOT NULL DEFAULT ''",
            "product_id": "TEXT",
            "origin": "TEXT NOT NULL DEFAULT 'manual'",
            "source_interaction_id": "TEXT",
            "source_at": "TEXT NOT NULL DEFAULT ''",
            "expires_at": "TEXT",
        }.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_jobs (
                interaction_id TEXT PRIMARY KEY REFERENCES interactions ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, saved_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS memory_jobs_pending ON memory_jobs(status,available_at);
            CREATE TABLE IF NOT EXISTS memory_forgetting (
                user_id TEXT NOT NULL, key TEXT NOT NULL,
                forgotten_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(user_id,key)
            );
            CREATE TRIGGER IF NOT EXISTS forget_derived_memory BEFORE DELETE ON interactions
            BEGIN
                INSERT OR REPLACE INTO memory_forgetting(user_id,key)
                    SELECT user_id,key FROM memories WHERE origin='automatic'
                    AND user_id=OLD.user_id AND source_interaction_id=OLD.interaction_id;
                DELETE FROM memories WHERE origin='automatic' AND user_id=OLD.user_id
                    AND source_interaction_id=OLD.interaction_id;
            END;
            CREATE TABLE IF NOT EXISTS memory_learning_state (
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TRIGGER IF NOT EXISTS queue_memory_after_reply AFTER UPDATE OF status ON interactions
            WHEN NEW.status='completed' AND EXISTS(SELECT 1 FROM memory_learning_state WHERE id=1 AND enabled=1)
            BEGIN
                INSERT OR IGNORE INTO memory_jobs(interaction_id) VALUES(NEW.interaction_id);
            END;
        """)
        await db.executemany(
            "INSERT INTO products(product_id,name,docs_url) VALUES(?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET name=excluded.name,docs_url=excluded.docs_url",
            [(p["id"], p["name"], p["docs_url"]) for p in products()],
        )
        # Changes from explicit saves, extraction, forgetting and session deletion all enqueue
        # projection work in the same SQLite transaction. Elasticsearch may be temporarily offline.
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_search_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                key TEXT NOT NULL, UNIQUE(user_id,key)
            );
            CREATE TRIGGER IF NOT EXISTS memory_search_insert AFTER INSERT ON memories BEGIN
                DELETE FROM memory_search_jobs WHERE user_id=NEW.user_id AND key=NEW.key;
                INSERT INTO memory_search_jobs(user_id,key) VALUES(NEW.user_id,NEW.key);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_search_update AFTER UPDATE ON memories BEGIN
                DELETE FROM memory_search_jobs WHERE user_id=NEW.user_id AND key=NEW.key;
                INSERT INTO memory_search_jobs(user_id,key) VALUES(NEW.user_id,NEW.key);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_search_delete AFTER DELETE ON memories BEGIN
                DELETE FROM memory_search_jobs WHERE user_id=OLD.user_id AND key=OLD.key;
                INSERT INTO memory_search_jobs(user_id,key) VALUES(OLD.user_id,OLD.key);
            END;
        """)
        return cls(db)

    # Insert a running interaction with a UUID and the original question. Record selected and
    # text-mentioned product relationships; a textual mention alone does not establish actual
    # product usage.
    async def begin_interaction(self, user_id, session_id, question, product_id=None):
        identifier = str(uuid.uuid4())
        await self.db.execute(
            "INSERT INTO interactions(interaction_id,user_id,session_id,question,status) "
            "VALUES(?,?,?,?,'running')",
            (identifier, user_id, session_id, question),
        )
        relations = [(identifier, p, "mentioned") for p in mentioned_products(question)]
        if product_id:
            relations.append((identifier, product_id, "selected"))
        await self.db.executemany(
            "INSERT OR IGNORE INTO interaction_products VALUES(?,?,?)",
            relations,
        )
        return identifier

    # Persist the interaction outcome and completion time. When automatic learning is enabled,
    # the SQLite completion trigger queues extraction; this method does not call the model.
    async def finish_interaction(self, identifier, status):
        await self.db.execute(
            "UPDATE interactions SET status=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE interaction_id=?",
            (status, identifier),
        )

    # Persist each retrieved source, chunk, product, rank, and score against the interaction.
    # Also link retrieved products to the interaction without duplicating existing
    # relationships.
    async def record_retrieval(self, interaction_id, query, mode, hits):
        await self.db.executemany(
            "INSERT INTO retrievals(interaction_id,query,mode,source,chunk,product_id,rank,score) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    interaction_id,
                    query,
                    mode,
                    h["source"],
                    h["chunk"],
                    h.get("product_id"),
                    rank,
                    h["score"],
                )
                for rank, h in enumerate(hits, 1)
            ],
        )
        await self.db.executemany(
            "INSERT OR IGNORE INTO interaction_products VALUES(?,?,'retrieved')",
            [(interaction_id, h["product_id"]) for h in hits if h.get("product_id")],
        )

    # Validate product, status, evidence, and resolution fields, then upsert the incident
    # identified by user, session, and product. A resolved status requires resolution text; this
    # method does not independently verify the fix.
    async def record_incident(
        self,
        user_id,
        session_id,
        product_id,
        summary,
        status,
        resolution,
        evidence,
        helpful_source="unknown",
        contribution="unknown",
    ):
        if status not in {"open", "resolved"} or not summary.strip() or not evidence.strip():
            raise ValueError("Provide summary, supporting user quote and open/resolved status")
        if status == "resolved" and not resolution.strip():
            raise ValueError("Resolved incidents require a confirmed resolution")
        if max(len(summary), len(resolution), len(evidence)) > 2000:
            raise ValueError("Incident fields must be <=2000 characters")
        product_id = product_id.strip().lower()
        for product in products():
            if product_id == product["name"].lower():
                product_id = product["id"]
                break
        if product_id not in {p["id"] for p in products()}:
            raise ValueError("Unknown product")
        if helpful_source not in {"unknown", "rag", "mcp", "both", "neither"}:
            raise ValueError("Unknown helpful source")
        if contribution not in {"unknown", "helped", "solved", "not_helpful"}:
            raise ValueError("Unknown contribution")
        if contribution == "solved" and status != "resolved":
            raise ValueError("Solved feedback requires a resolved incident")
        prior = await (
            await self.db.execute(
                "SELECT assistance FROM incidents WHERE user_id=? AND session_id=? AND product_id=?",
                (user_id, session_id, product_id),
            )
        ).fetchone()
        assistance = json.loads(prior["assistance"]) if prior else []
        required = {"rag", "mcp"} if helpful_source == "both" else {helpful_source}
        if helpful_source not in {"unknown", "neither"} and not required.issubset(
            {source["type"] for source in assistance}
        ):
            raise ValueError("That source was not recorded for this incident; use unknown")
        # A tool being used is not proof it solved the issue. Attribute only user-confirmed sources.
        resolution_sources = [source for source in assistance if source["type"] in required]
        await self.db.execute(
            "INSERT INTO incidents(incident_id,user_id,session_id,product_id,summary,status,resolution,evidence,helpful_source,contribution,resolution_sources) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,session_id,product_id) DO UPDATE SET "
            "summary=excluded.summary,status=excluded.status,resolution=excluded.resolution,"
            "helpful_source=excluded.helpful_source,contribution=excluded.contribution,"
            "resolution_sources=excluded.resolution_sources,"
            "evidence=excluded.evidence,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (
                str(uuid.uuid4()),
                user_id,
                session_id,
                product_id,
                summary,
                status,
                resolution,
                evidence,
                helpful_source,
                contribution,
                json.dumps(resolution_sources if status == "resolved" else []),
            ),
        )
        return {"status": "saved", "product_id": product_id, "incident_status": status}

    # Persist actual successful tools against open incidents, keeping a bounded provenance list.
    # User-confirmed attribution is recorded separately when feedback arrives.
    async def record_assistance(self, user_id, session_id, product_id, sources):
        rows = await (
            await self.db.execute(
                "SELECT incident_id,assistance FROM incidents WHERE user_id=? AND session_id=? "
                "AND status='open' AND (? IS NULL OR product_id=?)",
                (user_id, session_id, product_id, product_id),
            )
        ).fetchall()
        for row in rows:
            combined = json.loads(row["assistance"])
            for source in sources:
                if source not in combined:
                    combined.append(source)
            await self.db.execute(
                "UPDATE incidents SET assistance=? WHERE incident_id=?",
                (json.dumps(combined[-30:]), row["incident_id"]),
            )
        return bool(rows)

    # Return recent interaction-product rows and incidents for one user, with an optional
    # product filter. Multiple relationship rows may refer to the same interaction; results are
    # not a distinct conversation list.
    async def history(self, user_id, product_id=None, limit=100):
        async with self.db.execute(
            "SELECT i.*,p.product_id,p.relationship,c.name AS product_name FROM interactions i "
            "LEFT JOIN interaction_products p USING(interaction_id) "
            "LEFT JOIN products c ON c.product_id=p.product_id "
            "WHERE i.user_id=? AND (? IS NULL OR p.product_id=?) "
            "ORDER BY i.created_at DESC,p.product_id,p.relationship LIMIT ?",
            (user_id, product_id, product_id, limit),
        ) as cursor:
            interactions = [dict(r) for r in await cursor.fetchall()]
        async with self.db.execute(
            "SELECT * FROM incidents WHERE user_id=? AND (? IS NULL OR product_id=?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, product_id, product_id, limit),
        ) as cursor:
            incidents = [dict(r) for r in await cursor.fetchall()]
            for incident in incidents:
                for field in ("assistance", "resolution_sources"):
                    incident[field] = json.loads(incident[field])
        return {"interactions": interactions, "incidents": incidents}

    # Delete the user's interactions and incidents for a session. Database triggers and foreign-
    # key cascades remove associated extraction jobs, retrievals, and derived automatic
    # memories; ADK session deletion is handled separately.
    async def delete_session_history(self, user_id, session_id):
        # SQLite triggers delete derived facts and jobs atomically with source interactions.
        await self.db.execute(
            "DELETE FROM interactions WHERE user_id=? AND session_id=?", (user_id, session_id)
        )
        await self.db.execute(
            "DELETE FROM incidents WHERE user_id=? AND session_id=?", (user_id, session_id)
        )

    # Validate and upsert an explicitly saved preference or takeaway by user and key. Mark it
    # manual with no expiry, detach automatic provenance, and clear any forgetting marker for
    # that key.
    async def save(self, user_id, key, value, kind, evidence):
        if kind not in {"preference", "takeaway"}:
            raise ValueError("kind must be preference or takeaway")
        if not key.strip() or len(key) > 100 or not value.strip() or len(value) > 1000:
            raise ValueError("Use a nonempty key <=100 and value <=1000 characters")
        if not evidence.strip() or len(evidence) > 1000:
            raise ValueError("Provide a short supporting user quote")
        await self.db.execute(
            """
            INSERT INTO memories(user_id,key,value,kind,evidence,source_at) VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value,
                kind=excluded.kind, evidence=excluded.evidence, origin='manual', expires_at=NULL, source_interaction_id=NULL,
                source_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
            (user_id, key, value, kind, evidence),
        )
        await self.db.execute(
            "DELETE FROM memory_forgetting WHERE user_id=? AND key=?", (user_id, key)
        )
        return {"status": "saved", "key": key}

    # Return up to fifty recently updated, unexpired memories for one user with their provenance
    # metadata. Keep expired records in the database while excluding them from recall.
    async def list(self, user_id):
        async with self.db.execute(
            "SELECT key,value,kind,evidence,updated_at,category,subject,attribute,product_id,origin,source_interaction_id,source_at,expires_at FROM memories "
            "WHERE user_id=? AND (expires_at IS NULL OR expires_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) ORDER BY updated_at DESC,key LIMIT 50",
            (user_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    # Persist a forgetting timestamp for the user/key even if no memory exists, then delete any
    # matching fact. Return the deletion count; the marker blocks stale queued extraction from
    # restoring it.
    async def forget(self, user_id, key):
        await self.db.execute(
            "INSERT INTO memory_forgetting(user_id,key) VALUES(?,?) ON CONFLICT(user_id,key) "
            "DO UPDATE SET forgotten_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (user_id, key),
        )
        cursor = await self.db.execute(
            "DELETE FROM memories WHERE user_id=? AND key=?", (user_id, key)
        )
        return {"deleted": cursor.rowcount}

    # Close this store's SQLite connection after callers have finished using it.
    async def close(self):
        await self.db.close()


# Rank active memory rows for the current question and product, then select at most twelve
# within a serialized-text budget. This bounds prompt size without changing stored memories.
def select_memory_context(rows, question, product_id=None):
    """Bound the model's profile context; prefer relevant app/product facts and preferences."""
    terms = set(re.findall(r"[a-z0-9_-]{3,}", question.lower()))

    # Score a fact using selected-product alignment, preference priority, and bounded query-term
    # matches in its subject, value, or product. Higher scores are selected first.
    def score(row):
        text = (
            f"{row.get('subject', '')} {row.get('value', '')} {row.get('product_id', '')}".lower()
        )
        return (
            (10 if product_id and row.get("product_id") == product_id else 0)
            + (5 if row["kind"] == "preference" else 0)
            + min(10, sum(term in text for term in terms))
        )

    selected, budget = [], 0
    for row in sorted(rows, key=score, reverse=True):
        size = len(json.dumps(row))
        if budget + size <= 6500:
            selected.append(row)
            budget += size
        if len(selected) == 12:
            break
    return selected
