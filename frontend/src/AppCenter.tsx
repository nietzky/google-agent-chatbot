import { useEffect, useState } from "react";
import { LayoutGrid, ArrowLeft } from "lucide-react";
import { api } from "./api";

type CatalogApp = { id: string; name: string; description: string; url: string };

/** Display the Elasticsearch catalog independently of personalized recommendations. */
export default function AppCenter() {
  const [apps, setApps] = useState<CatalogApp[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    // Abort navigation/retry requests so an older response cannot replace current data.
    api<{ applications: CatalogApp[] }>("/applications", "GET", undefined, controller.signal)
      .then(result => { if (!controller.signal.aborted) { setApps(result.applications); setState("ready"); } })
      .catch(() => { if (!controller.signal.aborted) setState("error"); });
    return () => controller.abort();
  }, [revision]);
  return <div className="app-center-page">
    <header className="app-center-header"><a href="/"><ArrowLeft size={18} /> Back to conversations</a><a href="/notifications">Notifications</a></header>
    <main className="app-center-content">
      <div className="app-center-title"><LayoutGrid size={32} /><h1>App Center</h1></div>
      <p>Explore applications for troubleshooting AWS products.</p>
      <p className="app-center-disclaimer">Demo catalog. These applications are examples, not installed tools or live AWS integrations.</p>
      {state === "loading" && <p role="status">Loading applications...</p>}
      {state === "error" && <div role="alert"><p>The application catalog is temporarily unavailable.</p><button onClick={() => setRevision(n => n + 1)}>Try again</button></div>}
      {state === "ready" && <>
        <p>{apps.length} applications</p>
        {!apps.length && <p>No applications are available yet.</p>}
        <div className="app-center-grid">{apps.map(app => <article key={app.id}>
          <span className="app-center-badge">Demo app</span>
          <h2><a href={app.url}>{app.name}</a></h2>
          <p>{app.description}</p>
          <a className="app-center-details" href={app.url} aria-label={`View ${app.name}`}>View application &rarr;</a>
        </article>)}</div>
      </>}
    </main>
  </div>;
}
