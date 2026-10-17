import { useCallback, useEffect, useMemo, useState } from "react";
import type { Api, CollectionArticle, CollectionDetail, CollectionSummary } from "./api";

const RELEVANCE: Record<number, [string, string]> = {
  3: ["Core", "rel-3"],
  2: ["Closely related", "rel-2"],
  1: ["Tangential", "rel-1"],
};

const SHOW = [
  [3, "Core only"],
  [2, "Core + closely related"],
  [1, "Everything found"],
] as const;

const usd = (n: number) => (n < 0.01 && n > 0 ? "<$0.01" : `$${n.toFixed(2)}`);

export default function Topics({ client }: { client: Api }) {
  const [saved, setSaved] = useState<CollectionSummary[]>([]);
  const [current, setCurrent] = useState<CollectionDetail | null>(null);
  const [topic, setTopic] = useState("");
  const [lang, setLang] = useState("en");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");

  const loadSaved = useCallback(() => {
    client.collections().then(setSaved).catch(() => undefined);
  }, [client]);
  useEffect(loadSaved, [loadSaved]);

  const open = useCallback(
    (id: number) => {
      setError("");
      client.collection(id).then(setCurrent).catch((e) => setError(String(e.message ?? e)));
    },
    [client],
  );

  const search = async (e: React.FormEvent) => {
    e.preventDefault();
    setSearching(true);
    setError("");
    try {
      const c = await client.createCollection(topic.trim(), lang);
      setCurrent(c);
      loadSaved();
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setSearching(false);
    }
  };

  return (
    <main className="page wide">
      <h2>Find articles on a topic</h2>
      <form className="run-form" onSubmit={search}>
        <label>
          Topic
          <input
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="e.g. Great Flood of 1862"
            disabled={searching}
          />
        </label>
        <label>
          Language
          <input value={lang} onChange={(e) => setLang(e.target.value)} size={4} disabled={searching} />
        </label>
        <button type="submit" className="primary" disabled={searching || topic.trim().length < 2}>
          {searching ? "Searching…" : "Find articles"}
        </button>
      </form>
      <p className="muted small">
        Articles come from Wikipedia's search, the topic's categories, and links from the topic's
        own article. A fast model then rates how central each one is, from its title and short
        description only. Searching takes up to a minute and costs about a cent.
      </p>
      {error && <p className="error">{error}</p>}

      {saved.length > 0 && (
        <div className="saved">
          <span className="muted small">Saved topics:</span>
          {saved.map((c) => (
            <button
              key={c.id}
              className={current?.id === c.id ? "chip active" : "chip"}
              onClick={() => open(c.id)}
            >
              {c.topic} <span className="muted">({c.selected_count}/{c.article_count})</span>
            </button>
          ))}
        </div>
      )}

      {current && (
        <CollectionView key={current.id} initial={current} client={client} onChange={loadSaved} />
      )}
    </main>
  );
}

function CollectionView({
  initial,
  client,
  onChange,
}: {
  initial: CollectionDetail;
  client: Api;
  onChange: () => void;
}) {
  const [c, setC] = useState(initial);
  const [minRelevance, setMinRelevance] = useState(2);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    client.collection(c.id).then(setC).catch(() => undefined);
  }, [client, c.id]);

  // While any of its checks are queued or running, keep the table up to date.
  const active = c.articles.some(
    (a) => a.last_run && (a.last_run.status === "queued" || a.last_run.status === "running"),
  );
  useEffect(() => {
    if (!active) return;
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [active, refresh]);

  const shown = useMemo(
    () => c.articles.filter((a) => a.relevance >= minRelevance),
    [c.articles, minRelevance],
  );
  const selected = c.articles.filter((a) => chosen.has(a.title));
  const estimate = selected.reduce((sum, a) => sum + a.estimated_cost_usd, 0);

  const toggle = (title: string) =>
    setChosen((prev) => {
      const next = new Set(prev);
      if (next.has(title)) next.delete(title);
      else next.add(title);
      return next;
    });

  const scan = async () => {
    const protectedCount = selected.filter((a) => a.edit_protection).length;
    const lines = [
      `Check ${selected.length} article${selected.length === 1 ? "" : "s"}?`,
      `Estimated cost: about ${usd(estimate)} in Claude API usage.`,
    ];
    if (protectedCount)
      lines.push(
        protectedCount === 1
          ? "One of them is edit-protected: only experienced editors can change it, and others must post edit requests."
          : `${protectedCount} of them are edit-protected: only experienced editors can change them, and others must post edit requests.`,
      );
    if (!window.confirm(lines.join("\n\n"))) return;
    setBusy(true);
    setMessage("");
    try {
      const r = await client.scanCollection(
        c.id,
        selected.map((a) => a.title),
      );
      const skipped = r.already_in_progress.length
        ? ` ${r.already_in_progress.length} already in progress.`
        : "";
      setMessage(
        `Queued ${r.queued} check${r.queued === 1 ? "" : "s"}.${skipped} They run in the worker (wcm worker must be running); results appear in the review queue.`,
      );
      setChosen(new Set());
      refresh();
      onChange();
    } catch (err) {
      setMessage(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const basis =
    c.cost_basis_runs > 0
      ? `${usd(c.cost_per_citation)} per citation, learned from ${c.cost_basis_runs} finished run${c.cost_basis_runs === 1 ? "" : "s"}`
      : `a default ${usd(c.cost_per_citation)} per citation (no finished runs yet)`;

  return (
    <section className="collection">
      <h3>
        {c.topic} <span className="muted small">· {c.article_count} articles found</span>
      </h3>
      <div className="filters inline">
        {SHOW.map(([value, label]) => (
          <button
            key={value}
            className={minRelevance === value ? "chip active" : "chip"}
            onClick={() => setMinRelevance(value)}
          >
            {label}
          </button>
        ))}
        <button className="link small" onClick={() => setChosen(new Set(shown.map((a) => a.title)))}>
          Select all shown
        </button>
        <button className="link small" onClick={() => setChosen(new Set())}>
          Clear
        </button>
      </div>

      <div className="table-wrap">
        <table className="topics">
          <thead>
            <tr>
              <th></th>
              <th>Relevance</th>
              <th>Article</th>
              <th className="col-size">Size</th>
              <th>Last check</th>
              <th className="num">Est. cost</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((a) => (
              <Row key={a.id} a={a} checked={chosen.has(a.title)} onToggle={() => toggle(a.title)} />
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Estimates use {basis}, and the article's size to guess its number of citations, so treat
        them as rough. Re-checking an article that hasn't changed costs almost nothing.
      </p>

      <div className="scanbar">
        <span>
          <strong>{selected.length}</strong> selected · estimated <strong>{usd(estimate)}</strong>
        </span>
        <button className="primary" disabled={busy || selected.length === 0} onClick={scan}>
          Check selected articles
        </button>
      </div>
      {message && <p className="small">{message}</p>}
    </section>
  );
}

function Row({ a, checked, onToggle }: { a: CollectionArticle; checked: boolean; onToggle: () => void }) {
  const [label, cls] = RELEVANCE[a.relevance] ?? ["?", ""];
  const r = a.last_run;
  return (
    <tr className={checked ? "chosen" : ""}>
      <td>
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={`Select ${a.title}`} />
      </td>
      <td>
        <span className={`rel ${cls}`}>{label}</span>
      </td>
      <td>
        <a href={a.url} target="_blank" rel="noreferrer">
          {a.title}
        </a>
        {a.edit_protection && (
          <span
            className="badge prot"
            title="Edit-protected: only editors with enough experience can edit it directly; others post edit requests on the talk page."
          >
            {a.edit_protection === "extendedconfirmed" ? "extended-confirmed" : a.edit_protection}
          </span>
        )}
        {a.description && <div className="muted small">{a.description}</div>}
        <div className="muted small">
          {a.relevance_note}
          {a.relevance_note && " · "}found via {a.found_via}
        </div>
      </td>
      <td className="small col-size">
        {a.size_bytes ? `${Math.round(a.size_bytes / 1000)} kB` : "—"}
      </td>
      <td className="small">
        {r ? (
          <>
            <span className={`status s-${r.status}`}>{r.status}</span>
            {r.status === "done" && (
              <div className="muted">
                {a.open_findings} source · {a.open_uncited} no-citation open · {usd(r.cost_usd)}
              </div>
            )}
          </>
        ) : (
          <span className="muted">never</span>
        )}
      </td>
      <td className="num small">{usd(a.estimated_cost_usd)}</td>
    </tr>
  );
}
