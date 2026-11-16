import { useEffect, useState } from "react";
import { api, type Memory, type Product } from "./api";

/** Turn stored extraction slugs into readable labels without guessing application names. */
function label(value: string) {
  return value.replace(/[_-]+/g, " ");
}

/**
 * Show saved context available to Cloud Help, grouped by its recorded application/subject.
 * Refresh after conversation changes and briefly poll outstanding extraction jobs. Requests
 * are cancelled on cleanup; a failed refresh hides stale facts instead of implying freshness.
 * Product filtering here changes only the display, not the backend's memory selection.
 */
export default function ApplicationContext({ product, revision, busy }: {
  product: Product | null;
  revision: number;
  busy: boolean;
}) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [learning, setLearning] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    /** Fetch current facts and worker state together, then poll only while jobs are pending. */
    async function load() {
      try {
        const [facts, status] = await Promise.all([
          api<Memory[]>("/memories", "GET", undefined, controller.signal),
          api<{ enabled: boolean; jobs: Record<string, number> }>(
            "/memories/status", "GET", undefined, controller.signal,
          ),
        ]);
        if (controller.signal.aborted) return;
        setMemories(facts);
        setState("ready");
        const pending = status.enabled && ((status.jobs.queued || 0) + (status.jobs.running || 0) > 0);
        setLearning(pending);
        if (pending && ++attempts < 20) timer = setTimeout(() => void load(), 3000);
      } catch {
        if (!controller.signal.aborted) {
          setState("error");
          setLearning(false);
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision, busy, refresh]);

  const visible = memories.filter((fact) =>
    !product || !fact.product_id || fact.product_id === product.id,
  );
  const groups = new Map<string, Memory[]>();
  for (const fact of visible) {
    const subject = fact.subject || "General preferences";
    groups.set(subject, [...(groups.get(subject) || []), fact]);
  }

  return (
    <section className="application-context" aria-label="Application context">
      <div className="context-heading">
        <strong>Application context</strong>
        <button type="button" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>
          Refresh
        </button>
      </div>
      {state === "loading" && <p role="status">Loading saved context…</p>}
      {state === "error" && <p role="status">Saved context is unavailable. You can still chat. Try Refresh.</p>}
      {state === "ready" && (visible.length ? (
        <details>
          <summary>
            <span className="context-chips">
              {[...groups.keys()].slice(0, 3).map((subject) => <span key={subject}>{label(subject)}</span>)}
            </span>
            <span className="context-count">{visible.length} saved {visible.length === 1 ? "detail" : "details"} · View</span>
          </summary>
          <div className="context-details">
            <p>Saved context available to Cloud Help. Each answer may use a subset.
              {product && ` Showing general details and ${product.name} context.`}
              {" "}Ask Cloud Help to correct or forget anything.</p>
            {[...groups].map(([subject, facts]) => (
              <div className="context-group" key={subject}>
                <h3>{label(subject)}</h3>
                <ul>{facts.map((fact) => (
                  <li key={fact.key}>
                    <span className="context-category">{label(fact.category || fact.kind)}</span>
                    <span>{fact.value}</span>
                    {fact.product_id && <small>{fact.product_id}</small>}
                  </li>
                ))}</ul>
              </div>
            ))}
          </div>
        </details>
      ) : <p>{product ? "No saved context for this product yet." : "No saved application context yet."} Tell Cloud Help about your app, stack, or environment as you chat.</p>)}
      {learning && <p role="status">Learning from recent conversations… Refresh to check again.</p>}
    </section>
  );
}
