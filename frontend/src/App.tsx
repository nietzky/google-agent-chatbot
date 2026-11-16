import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Bell,
  Check,
  LayoutGrid,
  PanelLeftClose,
  PanelLeftOpen,
  MessageCircle,
  MoreHorizontal,
  Plus,
  Search,
  Square,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import SuggestedApplications from "./SuggestedApplications";
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
  const [memoryRevision, setMemoryRevision] = useState(0);
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
      const saved = localStorage.getItem("cloud-help-sidebar-open");
      return saved === "true";
    } catch {
      return false;
    }
  });
  /** Persist the user's sidebar preference without making browser storage required. */
  function toggleSidebar() {
    setSidebarOpen((open) => {
      try {
        localStorage.setItem("cloud-help-sidebar-open", String(!open));
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
    const transcript = bottom.current?.closest("main");
    const latest = transcript?.querySelector<HTMLElement>(".thinking") || transcript?.querySelector<HTMLElement>(".message:last-child");
    if (transcript && latest) {
      const end = latest.getBoundingClientRect().bottom - transcript.getBoundingClientRect().top + transcript.scrollTop;
      transcript.scrollTo({ top: Math.max(0, end - transcript.clientHeight + 24), behavior: "smooth" });
    }
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
      className={`app ${sidebarOpen ? "sidebar-expanded" : "sidebar-collapsed"} ${messages.length ? "has-messages" : "welcome-mode"}`}
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
          {sidebarOpen && <span>Cloud Help</span>}
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
        <a className="app-center-link" href="/app-center" aria-label="App Center" title="App Center">
          <LayoutGrid size={20} />
          {sidebarOpen && <span>App Center</span>}
        </a>
        <a className="app-center-link" href="/notifications" aria-label="Notifications" title="Notifications">
          <Bell size={20} />
          {sidebarOpen && <span>Notifications</span>}
        </a>
        <div id="conversation-history" hidden={!sidebarOpen}>
          <p className="sidebar-section-label">Conversations</p>
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
                  <span className="conversation-title" title={conversation.title}>{conversation.title}</span>
                </button>
                <details className="conversation-actions" onBlur={(event) => {
                  // Close actions when keyboard focus or a click moves outside this row menu.
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) event.currentTarget.open = false;
                }} onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.currentTarget.open = false;
                    event.currentTarget.querySelector("summary")?.focus();
                  }
                }}>
                  <summary aria-label={`Conversation actions for ${conversation.title}`} title="Conversation actions">
                    <MoreHorizontal size={17} />
                  </summary>
                <button
                  className="conversation-delete"
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
                  Delete conversation
                </button>
                </details>
              </div>
            ))}
            {!conversations.length && (
              <p className="no-results">Your conversations will appear here.</p>
            )}
          </div>
        </div>
      </aside>
      <header className="topbar">
        <a className="brand" href="/" aria-label="Cloud Help home">
          <span className="brand-icon">
            <MessageCircle size={21} />
          </span>
          Cloud Help
          <span className="brand-description">Your IT support companion</span>
        </a>
        <nav aria-label="Chat controls">
          <button
            className="products-button"
            aria-label="Supported products"
            onClick={() => {
              setModal("products");
              void loadProducts();
            }}
          >
            <span>Supported products</span>
            <LayoutGrid size={23} />
          </button>
        </nav>
      </header>
      <main className={messages.length ? "conversation" : "conversation empty"}>
        {!messages.length && (
          <section className="welcome">
            <h1>What can we help you solve?</h1>
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
                  Cloud Help
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
        {messages.length > 0 && <>
        <SuggestedApplications revision={messages.length} busy={busy} onForget={() => setMemoryRevision(n => n + 1)} />
        <ApplicationContext
          product={selected}
          revision={messages.length + memoryRevision}
          busy={busy}
        />
        </>}
        <div ref={bottom} />
      </main>
      <div className="composer-area">
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
            aria-label="Message Cloud Help"
            placeholder="Ask a question or describe an issue…"
            value={input}
            maxLength={2000}
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
            <span className="character-count" aria-label="Character count">
              {input.length} / 2000
            </span>
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

        {!messages.length && (
          <p className="welcome-description">
            Cloud Help remembers useful technical context. Ask it to forget anything.
            Never share passwords or access keys.
          </p>
        )}
        {!messages.length && <>
        <SuggestedApplications revision={messages.length} busy={busy} onForget={() => setMemoryRevision(n => n + 1)} />
        <ApplicationContext
          product={selected}
          revision={messages.length + memoryRevision}
          busy={busy}
        />
        </>}
        {messages.length > 0 && <p className="composer-note">
          Cloud Help remembers useful technical context. Ask it to forget anything.
          Never share passwords or access keys.
        </p>}
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
