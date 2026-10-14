import { useCallback, useEffect, useState } from "react";
import type { Api, FindingDetail, FindingSummary, Quote } from "./api";

const STATUSES = [
  ["open", "Open"],
  ["needs_research", "Needs research"],
  ["confirmed", "Confirmed"],
  ["dismissed", "Dismissed"],
] as const;

const VERDICT_LABEL: Record<string, string> = {
  supported: "Supported",
  partially_supported: "Partly supported",
  not_supported: "Not in source",
  contradicted: "Source contradicts",
  source_insufficient: "Source insufficient",
  source_unavailable: "Source unavailable",
};

export default function ReviewQueue({ client }: { client: Api }) {
  const [status, setStatus] = useState<string>("open");
  const [items, setItems] = useState<FindingSummary[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    client
      .findings(status)
      .then((list) => {
        setItems(list);
        setError("");
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [client, status]);

  useEffect(load, [load]);

  return (
    <main className="queue">
      <aside className="list">
        <div className="filters" role="tablist">
          {STATUSES.map(([value, label]) => (
            <button
              key={value}
              className={status === value ? "chip active" : "chip"}
              onClick={() => {
                setStatus(value);
                setSelected(null);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {error && <p className="error">{error}</p>}
        {items.length === 0 && !error && <p className="muted pad">Nothing here.</p>}
        <ul>
          {items.map((f) => (
            <li key={f.id}>
              <button
                className={selected === f.id ? "item selected" : "item"}
                onClick={() => setSelected(f.id)}
              >
                <span className="item-head">
                  <span className={`prio prio-${f.priority}`}>{f.priority}</span>
                  <span className="muted">{f.article}</span>
                </span>
                <span className="claim-line">{f.claim}</span>
                <span className="muted small">{VERDICT_LABEL[f.verdict] ?? f.verdict}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="detail">
        {selected ? (
          <FindingView key={selected} id={selected} client={client} onReviewed={load} />
        ) : (
          <p className="muted pad">Select a finding to review it.</p>
        )}
      </section>
    </main>
  );
}

function Highlighted({ excerpt, quote }: { excerpt: string; quote: string }) {
  const norm = (s: string) => s.replace(/\s+/g, " ");
  const text = norm(excerpt);
  const i = text.toLowerCase().indexOf(norm(quote).toLowerCase());
  if (i < 0) return <>{text}</>;
  const j = i + norm(quote).length;
  return (
    <>
      {text.slice(0, i)}
      <mark>{text.slice(i, j)}</mark>
      {text.slice(j)}
    </>
  );
}

function QuoteBlock({ q }: { q: Quote }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`quote ${q.kind}`}>
      <div className="quote-head">
        <span>{q.kind === "supporting" ? "Source says (supports)" : "Source says (contradicts)"}</span>
        <span className={q.verified ? "badge ok" : "badge bad"}>
          {q.verified ? "found in source" : "NOT found in source"}
        </span>
      </div>
      <blockquote>{q.text}</blockquote>
      {q.translation && <p className="muted small">Translation: {q.translation}</p>}
      {q.excerpt && (
        <>
          <button className="link small" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} surrounding text
          </button>
          {open && (
            <p className="excerpt">
              <Highlighted excerpt={q.excerpt} quote={q.text} />
            </p>
          )}
        </>
      )}
    </div>
  );
}

function FindingView({ id, client, onReviewed }: { id: number; client: Api; onReviewed: () => void }) {
  const [f, setF] = useState<FindingDetail | null>(null);
  const [note, setNote] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    client.finding(id).then(setF).catch((e) => setError(String(e.message ?? e)));
  }, [client, id]);
  useEffect(load, [load]);

  useEffect(() => {
    if (f?.status === "confirmed") client.talkDraft(id).then(setDraft).catch(() => setDraft(null));
    else setDraft(null);
  }, [client, id, f?.status]);

  if (error) return <p className="error pad">{error}</p>;
  if (!f) return <p className="muted pad">Loading…</p>;

  const decide = async (decision: string) => {
    setBusy(true);
    try {
      await client.review(id, decision, note);
      setNote("");
      load();
      onReviewed();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="finding">
      <header>
        <h2>{f.article}</h2>
        <p className="muted">
          {f.section} ·{" "}
          <a href={f.permalink} target="_blank" rel="noreferrer">
            revision {f.rev_id}
          </a>{" "}
          · finding #{f.id} · status <strong>{f.status.replace("_", " ")}</strong>
        </p>
      </header>

      <h3>Article text</h3>
      {f.context_before && <p className="context">{f.context_before}</p>}
      <p className="claim">{f.claim}</p>

      <h3>Citation</h3>
      <p className="citation">{f.citation_text}</p>
      <p className="small">
        {f.source_url && (
          <a href={f.source_url} target="_blank" rel="noreferrer">
            {f.source_via_archive ? "Archived copy checked" : "Source checked"}
          </a>
        )}
        {f.source_fetched_at && (
          <span className="muted"> · fetched {new Date(f.source_fetched_at).toLocaleString()}</span>
        )}
      </p>

      <h3>
        Tool's assessment:{" "}
        <span className={`verdict v-${f.verdict}`}>{VERDICT_LABEL[f.verdict] ?? f.verdict}</span>
      </h3>
      <p className="muted small">
        Model confidence {f.confidence != null ? Math.round(f.confidence * 100) + "%" : "n/a"} ·{" "}
        {f.served_model ?? f.model} · {f.prompt_version}. Check the source yourself; the model can be wrong.
      </p>
      <p>{f.explanation}</p>
      {f.unsupported_parts.length > 0 && (
        <>
          <p className="small"><strong>Parts not found in the source:</strong></p>
          <ul className="parts">
            {f.unsupported_parts.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </>
      )}
      {f.quotes.map((q, i) => (
        <QuoteBlock key={i} q={q} />
      ))}

      <h3>Your decision</h3>
      <textarea
        placeholder="Your own note: what you checked and what you found. This becomes the summary in the talk-page draft."
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={3}
      />
      <div className="actions">
        <button disabled={busy} className="primary" onClick={() => decide("confirmed")}>
          Confirm: source doesn't support it
        </button>
        <button disabled={busy} onClick={() => decide("needs_research")}>
          Needs more research
        </button>
        <button disabled={busy} onClick={() => decide("dismissed")}>
          Dismiss: tool is wrong
        </button>
      </div>

      {f.reviews.length > 0 && (
        <>
          <h3>History</h3>
          <ul className="history">
            {f.reviews.map((r, i) => (
              <li key={i}>
                <strong>{r.reviewer}</strong> — {r.decision.replace("_", " ")}{" "}
                <span className="muted small">{new Date(r.created_at).toLocaleString()}</span>
                {r.note && <div className="small">{r.note}</div>}
              </li>
            ))}
          </ul>
        </>
      )}

      {draft && (
        <>
          <h3>Talk-page draft</h3>
          <p className="muted small">
            A starting point only. Re-check the source, rewrite it in your own words, say you used a
            checking tool, and post one issue at a time from your own account.
          </p>
          <textarea className="draft" readOnly value={draft} rows={14} />
          <button onClick={() => navigator.clipboard?.writeText(draft)}>Copy draft</button>
        </>
      )}
    </article>
  );
}
