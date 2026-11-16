import { useEffect, useState } from "react";
import { Bell, ArrowLeft } from "lucide-react";
import { api } from "./api";

type Notice = { id: string; title: string; description: string; product_id: string; severity: string; created_at: string };

/** Render demo notifications from Elasticsearch with explicit loading, empty, and retry states. */
export default function Notifications() {
  const [items, setItems] = useState<Notice[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    // Cancel obsolete navigation/refresh requests to prevent stale results replacing new ones.
    api<{ notifications: Notice[] }>("/notifications", "GET", undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) { setItems(result.notifications); setState("ready"); } })
      .catch(() => { if (!controller.signal.aborted) setState("error"); });
    return () => controller.abort();
  }, [revision]);
  return <div className="app-center-page">
    <header className="app-center-header"><a href="/"><ArrowLeft size={18} /> Back to conversations</a><a href="/app-center">App Center</a></header>
    <main className="app-center-content">
      <div className="app-center-title"><Bell size={32} /><h1>Notifications</h1></div>
      <p className="app-center-disclaimer">Demo notifications. These are not live alerts from your AWS account.</p>
      <button className="notification-refresh" disabled={state === "loading"} onClick={() => setRevision(n => n + 1)}>Refresh</button>
      {state === "loading" && <p role="status">Loading notifications...</p>}
      {state === "error" && <div role="alert"><p>Notifications are temporarily unavailable.</p><button onClick={() => setRevision(n => n + 1)}>Try again</button></div>}
      {state === "ready" && <>
        <p>{items.length} demo notifications</p>
        {!items.length && <p>No notifications yet.</p>}
        <div className="app-center-grid">{items.map(item => <article key={item.id}>
          <span className={`notification-severity severity-${item.severity}`}>Demo / {item.severity}</span>
          <h2>{item.title}</h2><p>{item.description}</p>
          <small>{item.product_id.toUpperCase()} &middot; <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></small>
        </article>)}</div>
      </>}
    </main>
  </div>;
}
