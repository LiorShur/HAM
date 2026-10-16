import { useCallback, useEffect, useState } from "react";
import type { Api, FindingDetail, FindingSummary, Quote, SourceResult } from "./api";

const STATUSES = [
  ["open", "Open"],
  ["needs_research", "Needs research"],
  ["confirmed", "Confirmed"],
  ["dismissed", "Dismissed"],
  ["superseded", "Superseded"],
] as const;

function findingLabel(f: FindingSummary): string {
  const base = VERDICT_LABEL[f.verdict] ?? f.verdict;
  if (f.needs_offline_check) return `${base} · check offline source`;
  if (f.severity === "minor") return `${base} · minor`;
  return base;
}

const VERDICT_LABEL: Record<string, string> = {
  supported: "Supported",
  partially_supported: "Partly supported",
  not_supported: "Not in sources",
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
                <span className="muted small">{findingLabel(f)}</span>
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

function QuoteBlock({ q, many }: { q: Quote; many: boolean }) {
  const [open, setOpen] = useState(false);
  const who = many && q.source_number ? `Source ${q.source_number}` : "Source";
  return (
    <div className={`quote ${q.kind}`}>
      <div className="quote-head">
        <span>
          {who} says ({q.kind === "supporting" ? "supports" : "contradicts"})
        </span>
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

const SOURCE_RESULT: Record<string, string> = {
  supported: "supports it",
  partially_supported: "supports part of it",
  not_supported: "doesn't say this",
  contradicted: "says something different",
  source_insufficient: "couldn't read enough to tell",
  source_unavailable: "couldn't be checked online",
};

function SourceRow({ src }: { src: SourceResult }) {
  return (
    <li>
      <p className="citation">{src.citation_text}</p>
      <p className="small">
        <span className={`verdict v-${src.verdict ?? "none"}`}>
          {src.verdict ? SOURCE_RESULT[src.verdict] ?? src.verdict : "not checked"}
        </span>
        {src.source_url && (
          <>
            {" · "}
            <a href={src.source_url} target="_blank" rel="noreferrer">
              {src.source_via_archive ? "archived copy checked" : "source checked"}
            </a>
          </>
        )}
        {src.source_fetched_at && (
          <span className="muted"> · fetched {new Date(src.source_fetched_at).toLocaleString()}</span>
        )}
      </p>
    </li>
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

      <h3>{f.sources.length === 1 ? "Citation" : `Citations (${f.sources.length})`}</h3>
      <ol className="sources">
        {f.sources.map((src) => (
          <SourceRow key={src.citation_id} src={src} />
        ))}
      </ol>

      <h3>
        Tool's assessment:{" "}
        <span className={`verdict v-${f.verdict}`}>{VERDICT_LABEL[f.verdict] ?? f.verdict}</span>
        {f.severity === "minor" && <span className="badge"> minor</span>}
      </h3>
      {f.needs_offline_check && (
        <p className="notice">
          The sources that could be read don't cover all of this, but it also cites a source
          that isn't online. Check that source before treating this as a problem.
        </p>
      )}
      <p className="muted small">
        {f.assessment === "claim" ? "All readable sources judged together. " : ""}
        Model confidence {f.confidence != null ? Math.round(f.confidence * 100) + "%" : "n/a"} ·{" "}
        {f.served_model ?? f.model} · {f.prompt_version}. Check the sources yourself; the model can be
        wrong.
      </p>
      <p>{f.explanation}</p>
      {f.unsupported_parts.length > 0 && (
        <>
          <p className="small"><strong>Parts not found in the sources:</strong></p>
          <ul className="parts">
            {f.unsupported_parts.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </>
      )}
      {f.quotes.map((q, i) => (
        <QuoteBlock key={i} q={q} many={f.sources.length > 1} />
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
          Confirm: not supported by its sources
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
