export type Message = { id: string; role: "user" | "assistant"; text: string };
export type Conversation = { id: string; title: string; updated_at: number };
export type Memory = {
  key: string;
  value: string;
  kind: string;
  evidence: string;
  updated_at: string;
  category?: string | null;
  subject?: string | null;
  attribute?: string | null;
  product_id?: string | null;
  origin?: string;
};
export type Source = {
  id: string;
  title: string;
  url: string | null;
  chunks: number;
};
export type Library = { available: boolean; sources: Source[]; chunks: number };
export type StreamEvent = {
  label?: string;
  message?: string;
} & Partial<Message>;

/** Return successful responses; convert HTTP errors into an API detail or readable fallback. */
async function check(response: Response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${response.status}). Please try again.`,
    );
  }
  return response;
}

/** Send JSON to a same-origin API route, supporting cancellation and empty HTTP 204 responses. */
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await check(
    await fetch(`/api${path}`, {
      method,
      signal,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
  return response.status === 204 ? (undefined as T) : response.json();
}

/**
 * Post a turn with optional product context and deliver decoded server-sent events.
 * Buffer incomplete network chunks, ignore heartbeats, and reject errors or a missing done event.
 * Always cancel and release the stream reader when processing ends.
 */
export async function streamMessage(
  id: string,
  text: string,
  signal: AbortSignal,
  onEvent: (event: string, payload: StreamEvent) => void,
  productId?: string,
) {
  const response = await check(
    await fetch(`/api/conversations/${id}/messages`, {
      method: "POST",
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, product_id: productId || null }),
    }),
  );
  if (!response.body)
    throw new Error("Streaming is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "",
    completed = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let end;
      // Network chunks can contain multiple events or only part of an SSE frame.
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const lines = block.split("\n");
        const event = lines
          .find((line) => line.startsWith("event:"))
          ?.slice(6)
          .trim();
        const data = lines
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("\n");
        if (!event || !data) continue;
        const payload = JSON.parse(data) as StreamEvent;
        if (event === "error")
          throw new Error(payload.message || "The response failed.");
        if (event === "done") completed = true;
        onEvent(event, payload);
      }
      if (done) break;
    }
    if (!completed)
      throw new Error("The connection ended before the response completed.");
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export type Product = {
  id: string;
  name: string;
  docs_url: string;
  chunks: number;
};
export type ProductCatalog = { available: boolean; products: Product[] };
