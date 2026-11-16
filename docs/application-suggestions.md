# Evidence-backed application suggestions

`cloudhelp-applications-v1` stores ten fictional demo applications. Each source document has
only `name` and `description`; its Elasticsearch document ID is the matching AWS product ID.
The versioned seed is `knowledge/applications.json`. Run
`python scripts/seed_applications.py` from the project root to create/upsert the catalog.
No credentials, user facts, or invented external application URLs are stored in this index.
These apps are examples, not real diagnostic integrations.

## How recommendations work

`GET /api/applications/suggested` reads the configured user's active SQLite memories.
It accepts only `interest` and `product_usage` categories with a recorded product ID and
nonempty supporting evidence. It joins these exact IDs to Elasticsearch using an IDs query;
it does not claim that semantic similarity proves usage. The latest eligible facts rank first,
with at most three apps shown. The existing memory list exposes up to 50 recent active facts,
so older facts outside that window do not contribute. Empty eligibility returns no suggestions.
Elasticsearch failure returns `available: false`, never fabricated recommendations.

Preferences remain in the existing ADK-backed SQLite memory schema: user ID, category,
product ID, value, evidence, source interaction/time, update time, and expiry. Automatic
extraction applies the existing confidence/evidence safeguards; its interpretation can still
be wrong. The UI shows the supporting quote so users can review it. Searches alone and app
clicks are not treated as preferences. Correct facts through chat or use Forget this fact.
Forgetting uses the existing deletion and relearning-suppression mechanism. The next fetch
recomputes recommendations; another eligible fact can still support the same app.

The frontend refreshes after replies and polls pending extraction every three seconds for
up to 20 attempts. Reloading also refreshes. Each app links to a local, escaped HTML catalog
page at `/applications/{product_id}`, explicitly labeled fictional. No AWS operations occur.
`SuggestedApplications.tsx` owns suggestions; `ApplicationContext.tsx` displays saved context.
Each supporting fact includes its date and quote under Why this app?.

This remains a single-user local application (`WEB_USER_ID`); it is not a multi-tenant
authenticated service. Recommendations always use the server-configured identity.

## Validation

`tests/test_applications.py` checks eligibility, exact ID matching, user scoping, evidence
preservation, and removal of suggestions after eligible facts disappear. Run alongside the
backend suite, then build the React frontend with `npm run build`.

## App Center

Open `/app-center` or use App Center above Conversations in the sidebar.
The React page reads `GET /api/applications`, which queries the live Elasticsearch
index (up to 1,000 documents) and sorts cards by name. All ten fictional apps
are visible regardless of user memories. Each card links to its catalog detail page.
Loading, empty, and retryable unavailable states are explicit; no fabricated fallback catalog is used.

The local installation now uses concrete `cloudhelp-*` Elasticsearch indices. The migration
preserved document IDs, source contents, mappings and shard settings, verified every copied
document, and backed up the previous indices outside the repository before removing them.
SQLite memory and conversation databases remain unchanged.
