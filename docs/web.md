# Cloud Help web API and frontend

React/TypeScript is served by FastAPI at http://127.0.0.1:8000/. Build the frontend with
`npm ci && npm run build` in `frontend/` (run as separate commands in PowerShell if needed).
Start `python -m uvicorn chatbot.api:app --host 127.0.0.1 --port 8000` from the project root.
The current workspace Python is `../../.venv/Scripts/python.exe`.

The UI contains a support conversation, product picker and compact conversation history.
Select a product to constrain local RAG. Clear the selection to search across products.
Enter sends; Shift+Enter adds a line; Stop cancels the turn. History survives API restarts.
Memory is managed through natural language and API endpoints instead of a separate UI panel.

| Route | Purpose |
| --- | --- |
| `GET /api/health` | API liveness (not a dependency readiness assertion) |
| `GET /api/products` | Products actually present in the active index, with chunk counts |
| `GET /api/library` | Indexed documentation pages, URLs and chunk counts |
| `GET, POST /api/conversations` | List/create current-user conversations |
| `GET /api/conversations/{id}/messages` | User text and final orchestrator answers |
| `POST /api/conversations/{id}/messages` | SSE turn: `{"text":"...","product_id":"s3"}` |
| `DELETE /api/conversations/{id}` | Delete conversation and associated product history |
| `GET /api/product-history?product_id=s3` | Product interaction and incident records |
| `GET, DELETE /api/memories` | Inspect/forget explicit and automatically learned technical memories |
| `GET /api/memories/status` | Automatic extraction enabled state and current-user job counts |

SSE events are `status`, `message`, `done`, or `error`, with heartbeat comments. Internal
prompts, hidden reasoning, raw tool payloads and provider exception strings are not exposed.
The API rejects concurrent turns for the same conversation (409), limits total active turns
to three (429), validates product IDs, and cancels model/tool work on disconnect.

Automatic hybrid retrieval runs for selected/recognized AWS product questions before model
generation. Sources and ranks are recorded in SQLite. The orchestrator may request specialist
refinements or live official MCP documentation. A callback preserves related retrieved links
in the final response when the model omits them; these are labeled related documentation,
not a guarantee that every generated claim has been verified.

This sample binds localhost and uses one configured server-side identity. CORS/host checks
are restricted to local origins. It has no authentication and must not be exposed as a
multi-customer service without adding it. See README for tool setup and docs/memory.md for
schema and SQL examples.

After every completed reply, technical memory extraction runs in the background. It starts
with new replies and can take a few seconds to appear in `/api/memories`. It does not block
the response, and pending jobs survive restart. See docs/memory.md for scope, retention and controls.
