import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Check,
  ChevronDown,
  PanelLeftClose,
  PanelLeftOpen,
  MessageCircle,
  Plus,
  Search,
  Square,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import ApplicationContext from "./ApplicationContext";
import {
  api,
  streamMessage,
  type Conversation,
  type Message,
  type Product,
  type ProductCatalog,
} from "./api";

/**
 * Render the support conversation, composer, product picker, and saved conversation history.
 * Keep transient UI state here; the API owns persisted messages, product history, and memories.
 */
export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [session, setSession] = useState<string | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [selected, setSelected] = useState<Product | null>(null);
  const [catalogState, setCatalogState] = useState("Loading products…");
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [modal, setModal] = useState<"products" | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    try {
      const saved = localStorage.getItem("pingo-sidebar-open");
      return saved === null ? window.innerWidth > 760 : saved === "true";
    } catch {
      return window.innerWidth > 760;
    }
  });
  /** Persist the user's sidebar preference without making browser storage required. */
  function toggleSidebar() {
    setSidebarOpen((open) => {
      try {
        localStorage.setItem("pingo-sidebar-open", String(!open));
      } catch {
        /* Storage may be disabled. */
      }
      return !open;
    });
  }
  const dialog = useRef<HTMLDialogElement>(null);
  const controller = useRef<AbortController | null>(null);
  const sending = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  /** Replace history with server conversation metadata; callers handle request failures. */
  async function refreshHistory() {
    setConversations(await api<Conversation[]>("/conversations"));
  }
  /** Load indexed products and display their count or a readable availability/failure state. */
  async function loadProducts() {
    try {
      const result = await api<ProductCatalog>("/products");
      setProducts(result.products);
      setCatalogState(
        result.available
          ? `${result.products.length} AWS products`
          : "Product search temporarily unavailable",
      );
    } catch {
      setCatalogState("Product search temporarily unavailable");
    }
  }
  // Load picker data on mount; abort any active response when the component unmounts.
  useEffect(() => {
    void loadProducts();
    void refreshHistory().catch(() =>
      setError(
        "Could not load conversations. Check the connection and try again.",
      ),
    );
    return () => controller.current?.abort();
  }, []);
  // Synchronize React modal state with the native dialog and its focus handling.
  useEffect(() => {
    if (modal) dialog.current?.showModal();
    else dialog.current?.close();
  }, [modal]);
  // Keep the newest answer or progress update visible as the conversation grows.
  useEffect(() => {
    void bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, status]);

  /**
   * Ignore empty/duplicate submissions, create a session if needed, and show the user message.
   * Stream status and assistant messages with the selected product context; support cancellation.
   * On every exit, unlock the composer, refresh history, and restore keyboard focus.
   */
  async function send(text = input) {
    if (!text.trim() || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError("");
    setStatus("Thinking about your question");
    const abort = new AbortController();
    controller.current = abort;
    try {
      let id = session;
      if (!id) {
        const conversation = await api<Conversation>(
          "/conversations",
          "POST",
          { title: text.trim().slice(0, 80) },
          abort.signal,
        );
        id = conversation.id;
        setSession(id);
      }
      setInput("");
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "user", text: text.trim() },
      ]);
      await streamMessage(
        id,
        text.trim(),
        abort.signal,
        // Status events update progress; message events append complete assistant text.
        (event, payload) => {
          if (event === "status") setStatus(payload.label || "Working on it");
          if (event === "message" && payload.text)
            setMessages((current) => [
              ...current,
              {
                id: payload.id || crypto.randomUUID(),
                role: "assistant",
                text: payload.text!,
              },
            ]);
        },
        selected?.id,
      );
    } catch (caught) {
      if (abort.signal.aborted)
        setError(
          "Response stopped. You can send a follow-up when you’re ready.",
        );
      else
        setError(
          caught instanceof Error
            ? caught.message
            : "Something went wrong. Please try again.",
        );
    } finally {
      sending.current = false;
      controller.current = null;
      setBusy(false);
      setStatus("");
      void refreshHistory().catch(() => {});
      textarea.current?.focus();
    }
  }
  /** Load a saved transcript while idle, clear temporary product context, and close the picker. */
  async function openConversation(id: string) {
    if (busy) return;
    try {
      const loaded = await api<Message[]>(`/conversations/${id}/messages`);
      setMessages(loaded);
      setSession(id);
      if (window.innerWidth <= 760) setSidebarOpen(false);
      setSelected(null);
      setError("");
      setModal(null);
    } catch {
      setError("Could not open that conversation.");
    }
  }
  /** Clear local chat/composer state while idle; create the next server session only on send. */
  function newConversation() {
    if (busy) return;
    setSession(null);
    if (window.innerWidth <= 760) setSidebarOpen(false);
    setMessages([]);
    setSelected(null);
    setInput("");
    setError("");
    textarea.current?.focus();
  }

  return (
    <div
      className={`app ${sidebarOpen ? "sidebar-expanded" : "sidebar-collapsed"}`}
    >
      <aside className="chat-sidebar" aria-label="Conversation sidebar">
        <button
          className="sidebar-toggle"
          onClick={toggleSidebar}
          aria-label={
            sidebarOpen
              ? "Collapse conversation history"
              : "Expand conversation history"
          }
          aria-expanded={sidebarOpen}
          aria-controls="conversation-history"
        >
          {sidebarOpen ? (
            <PanelLeftClose size={20} />
          ) : (
            <PanelLeftOpen size={20} />
          )}
          {sidebarOpen && <span>Conversations</span>}
        </button>
        <button
          className="sidebar-new"
          aria-label="New conversation"
          disabled={busy}
          onClick={newConversation}
        >
          <Plus size={19} />
          {sidebarOpen && <span>New conversation</span>}
        </button>
        <div id="conversation-history" hidden={!sidebarOpen}>
          <div className="sidebar-history">
            {conversations.map((conversation) => (
              <div
                className={`history-row ${session === conversation.id ? "active" : ""}`}
                key={conversation.id}
              >
                <button
                  disabled={busy}
                  aria-current={
                    session === conversation.id ? "page" : undefined
                  }
                  onClick={() => void openConversation(conversation.id)}
                >
                  <span>
                    {conversation.title}
                    <small>
                      {new Date(
                        conversation.updated_at * 1000,
                      ).toLocaleDateString()}
                    </small>
                  </span>
                  <span>→</span>
                </button>
                <button
                  className="icon-button"
                  disabled={busy}
                  aria-label={`Delete ${conversation.title}`}
                  onClick={async () => {
                    // Confirm deletion, remove server history, then reconcile the open chat and list.
                    if (
                      !window.confirm(
                        "Delete this conversation and its product interaction history?",
                      )
                    )
                      return;
                    try {
                      await api(`/conversations/${conversation.id}`, "DELETE");
                      if (session === conversation.id) newConversation();
                      await refreshHistory();
                    } catch {
                      setError("Could not delete the conversation.");
                    }
                  }}
                >
                  <X size={15} />
                </button>
              </div>
            ))}
            {!conversations.length && (
              <p className="no-results">Your conversations will appear here.</p>
            )}
          </div>
        </div>
      </aside>
      <header className="topbar">
        <a className="brand" href="/" aria-label="Pingo home">
          <span className="brand-icon">
            <MessageCircle size={21} />
          </span>
          Pingo
          <span className="brand-description">Your IT support companion</span>
        </a>
        <nav aria-label="Chat controls">
          <button
            className="products-button"
            onClick={() => {
              setModal("products");
              void loadProducts();
            }}
          >
            <span>Supported products</span>
            <ChevronDown size={15} />
          </button>
        </nav>
      </header>
      <main className={messages.length ? "conversation" : "conversation empty"}>
        {!messages.length && (
          <section className="welcome">
            <div className="welcome-icon">
              <MessageCircle size={32} strokeWidth={1.6} />
              <span className="spark">✦</span>
            </div>
            <div className="eyebrow">A LITTLE HELP GOES A LONG WAY</div>
            <h1>Let’s get things working.</h1>
            <p>
              Tell me what’s happening. We’ll work through it together,
              <br className="desktop-break" /> one step at a time.
            </p>
            <div className="suggestions">
              {[
                "My S3 requests return AccessDenied",
                "Help me troubleshoot a slow EC2 instance",
                "Why is my Lambda function timing out?",
              ].map((prompt) => (
                <button
                  key={prompt}
                  onClick={() => void send(prompt)}
                  disabled={busy}
                >
                  {prompt}
                  <span>↗</span>
                </button>
              ))}
            </div>
            <button
              className="text-button"
              onClick={() => setModal("products")}
            >
              Explore {products.length || "our"} supported AWS products{" "}
              <span>→</span>
            </button>
          </section>
        )}
        <section
          className="messages"
          aria-label="Conversation"
          aria-live="polite"
          aria-relevant="additions"
        >
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              {message.role === "assistant" && (
                <div className="assistant-label">
                  <span className="tiny-logo">
                    <MessageCircle size={13} />
                  </span>
                  Pingo
                </div>
              )}
              <div className="message-content">
                <ReactMarkdown
                  components={{
                    a: ({ children, ...props }) => (
                      <a {...props} target="_blank" rel="noopener noreferrer">
                        {children}
                      </a>
                    ),
                  }}
                >
                  {message.text}
                </ReactMarkdown>
              </div>
            </article>
          ))}
        </section>
        {busy && (
          <div className="thinking" role="status">
            <span className="pulse" />
            {status}
          </div>
        )}
        <div ref={bottom} />
      </main>
      <div className="composer-area">
        <ApplicationContext
          product={selected}
          revision={messages.length}
          busy={busy}
        />
        {error && (
          <div className="error" role="alert">
            {error}
            <button aria-label="Dismiss message" onClick={() => setError("")}>
              <X size={15} />
            </button>
          </div>
        )}
        {selected && (
          <div className="product-context">
            <span>
              Discussing <strong>{selected.name}</strong>
            </span>
            <button
              disabled={busy}
              aria-label="Clear selected product"
              onClick={() => setSelected(null)}
            >
              <X size={14} />
            </button>
          </div>
        )}
        <form
          className="composer"
          onSubmit={(event) => {
            // Submit through the API without navigating or refreshing the browser page.
            event.preventDefault();
            void send();
          }}
        >
          <textarea
            ref={textarea}
            aria-label="Message Pingo"
            placeholder="Ask a question or describe an issue…"
            value={input}
            maxLength={12000}
            rows={2}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends; Shift+Enter adds a line, and IME composition remains uninterrupted.
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-bottom">
            <button
              type="button"
              className="context-button"
              disabled={busy}
              onClick={() => setModal("products")}
            >
              <Plus size={15} />
              {selected ? "Change product" : "Choose a product"}
            </button>
            {busy ? (
              <button
                type="button"
                className="send-button"
                aria-label="Stop response"
                onClick={() => controller.current?.abort()}
              >
                <Square size={16} fill="currentColor" />
              </button>
            ) : (
              <button
                type="submit"
                className="send-button"
                aria-label="Send message"
                disabled={!input.trim()}
              >
                <ArrowUp size={21} />
              </button>
            )}
          </div>
        </form>
        <p className="composer-note">
          Pingo remembers useful technical context. Ask it to forget anything.
          Never share passwords or access keys.
        </p>
      </div>
      <dialog
        ref={dialog}
        className="picker"
        onCancel={() => setModal(null)}
        onClose={() => setModal(null)}
        onClick={(event) => {
          // Dismiss backdrop clicks without closing when a child control is clicked.
          if (event.target === dialog.current) setModal(null);
        }}
        aria-labelledby="dialog-title"
      >
        <div className="dialog-heading">
          <div>
            <h2 id="dialog-title">Supported products</h2>
            <p>{`${catalogState} � Official documentation`}</p>
          </div>
          <button
            className="icon-button"
            aria-label="Close dialog"
            onClick={() => setModal(null)}
          >
            <X size={20} />
          </button>
        </div>
        {modal === "products" && (
          <>
            <label className="search">
              <Search size={18} />
              <input
                autoFocus
                aria-label="Search products"
                placeholder="Search AWS products…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <div className="product-list">
              {products
                .filter((p) =>
                  `${p.name} ${p.id}`
                    .toLowerCase()
                    .includes(query.toLowerCase()),
                )
                .map((product) => (
                  <button
                    key={product.id}
                    disabled={busy}
                    onClick={() => {
                      // Set context for future messages without sending a message immediately.
                      setSelected(product);
                      setModal(null);
                      textarea.current?.focus();
                    }}
                  >
                    <span>
                      {product.name}
                      <small>
                        {product.chunks} searchable documentation passages
                      </small>
                    </span>
                    {selected?.id === product.id ? (
                      <Check size={18} />
                    ) : (
                      <span className="product-arrow">↗</span>
                    )}
                  </button>
                ))}
              {!products.some((p) =>
                `${p.name} ${p.id}`.toLowerCase().includes(query.toLowerCase()),
              ) && (
                <p className="no-results">
                  {products.length
                    ? "No products match your search."
                    : catalogState}
                </p>
              )}
            </div>
            <p className="dialog-footnote">
              Selected overview and troubleshooting pages. You can also ask
              general IT questions.
            </p>
          </>
        )}
      </dialog>
    </div>
  );
}
