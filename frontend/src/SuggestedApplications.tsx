import { useEffect, useState } from "react";
import { api, type Memory } from "./api";
type App = { id: string; name: string; description: string; url: string; evidence: Memory[] };
/** Refresh evidence-backed recommendations after replies and while extraction is pending. */
export default function SuggestedApplications({revision, busy, onForget}: {revision: number; busy: boolean; onForget: () => void}) {
  const [apps, setApps] = useState<App[]>([]);
  const [status, setStatus] = useState("Loading suggestions...");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let attempts = 0;
    /** Read suggestions and extraction state together; cancel stale requests on cleanup. */
    async function load() {
      try {
        const [result, jobs] = await Promise.all([
          api<{available: boolean; applications: App[]}>("/applications/suggested", "GET", undefined, controller.signal),
          api<{jobs: Record<string, number>}>("/memories/status", "GET", undefined, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setApps(result.applications.slice(0, 3));
        setStatus(result.available ? "No evidence-backed suggestions yet. Tell Cloud Help which AWS products you use or want to learn about." : "Application suggestions are temporarily unavailable.");
        if ((jobs.jobs.queued || jobs.jobs.running) && ++attempts < 20) timer = setTimeout(() => void load(), 3000);
      } catch { if (!controller.signal.aborted) { setApps([]); setStatus("Application suggestions are temporarily unavailable."); } }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision, busy, refresh]);
  /** Delete the supporting memory and record a relearning suppression marker through the existing forget API. */
  async function forget(key: string) {
    try { await api("/memories", "DELETE", {key}); setApps([]); setRefresh(n => n + 1); onForget(); setError(""); }
    catch { setError("Could not forget this fact. Please try again."); }
  }
  return <section className="suggested-applications" aria-label="Suggested applications">
    <strong>Suggested applications</strong>
    {!apps.length && <p>{status}</p>}
    <div className="suggested-app-grid">{apps.map(app => <article key={app.id}>
      <a href={app.url} target="_blank" rel="noopener noreferrer">{app.name}</a>
      <details><summary>Why this app?</summary><p>{app.description}</p>{app.evidence.map(fact => <div className="app-evidence" key={fact.key}>
        <p>{fact.category}: {fact.value}</p><blockquote>{fact.evidence}</blockquote>
        <small>Saved {new Date(fact.updated_at).toLocaleDateString()}</small>
        <button disabled={busy} onClick={() => void forget(fact.key)}>Forget this fact</button>
      </div>)}</details>
    </article>)}</div>
    {error && <p role="alert">{error}</p>}
  </section>;
}
