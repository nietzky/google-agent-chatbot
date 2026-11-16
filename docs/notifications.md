# Demo notifications

Open `/notifications` from the sidebar or App Center. All records are fictional, shared
sample data, not real account alerts or personalized notifications. No messages are sent.

`GET /api/notifications` reads up to 1,000 documents from `cloudhelp-notifications-v1`, newest
first by `created_at`. Each record has title, description, product_id, severity, created_at,
and demo=true. Elasticsearch failures return 503; the UI offers retry rather than invented
fallback data. Refresh fetches the index again. There is no background polling or read state.

Run `python scripts/seed_notifications.py` from the project root to create the strict mapping
and upsert the ten records from `knowledge/notifications.json`. Stable IDs make reruns safe.
The seed does not modify app, RAG, or memory indices. Dates are fictional sample timestamps.
