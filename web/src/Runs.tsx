import { useCallback, useEffect, useState } from "react";
import type { Api, Run } from "./api";

export default function Runs({ client }: { client: Api }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [rev, setRev] = useState("");
  const [lang, setLang] = useState("en");

  const load = useCallback(() => {
    client.runs().then(setRuns).catch((e) => setError(String(e.message ?? e)));
  }, [client]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await client.createRun({
        title: title.trim() || undefined,
        rev_id: rev.trim() ? Number(rev) : undefined,
        lang,
      });
      setTitle("");
      setRev("");
      load();
    } catch (err) {
      setError(String((err as Error).message));
    }
  };

  return (
    <main className="page">
      <h2>Check an article</h2>
      <form className="run-form" onSubmit={submit}>
        <label>
          Article title
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Harbor Point Lighthouse" />
        </label>
        <label>
          or revision id
          <input value={rev} onChange={(e) => setRev(e.target.value.replace(/\D/g, ""))} inputMode="numeric" />
        </label>
        <label>
          Language
          <input value={lang} onChange={(e) => setLang(e.target.value)} size={4} />
        </label>
        <button type="submit" className="primary" disabled={!title.trim() && !rev.trim()}>
          Queue check
        </button>
      </form>
      <p className="muted small">
        Each check fetches every cited source and asks Claude about each claim, so a large article
        can take a while and cost a few dollars in API usage.
      </p>
      {error && <p className="error">{error}</p>}

      <h2>Recent runs</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Article</th>
              <th>Revision</th>
              <th>Status</th>
              <th>Claims</th>
              <th>Findings</th>
              <th>No citation</th>
              <th>Cost</th>
              <th>Unavailable sources</th>
              <th>By</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.title || "—"}</td>
                <td>{r.rev_id ?? r.requested_rev_id ?? "latest"}</td>
                <td>
                  <span className={`status s-${r.status}`}>{r.status}</span>
                  {r.error && <div className="error small">{r.error}</div>}
                </td>
                <td>{r.stats.claims ?? "—"}</td>
                <td>{r.stats.findings ?? 0}</td>
                <td>{r.stats.uncited_findings ?? 0}</td>
                <td>{r.cost_usd ? `$${r.cost_usd.toFixed(2)}` : "—"}</td>
                <td>{r.stats["verdict:source_unavailable"] ?? 0}</td>
                <td>{r.requested_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
