import { useEffect, useState } from "react";
import type { Api, Stats } from "./api";

export default function StatsView({ client }: { client: Api }) {
  const [s, setS] = useState<Stats | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    client.stats().then(setS).catch((e) => setError(String(e.message ?? e)));
  }, [client]);

  if (error) return <p className="error pad">{error}</p>;
  if (!s) return <p className="muted pad">Loading…</p>;

  const tiles: [string, string | number][] = [
    ["Articles checked", s.articles],
    ["Claim checks", s.verifications],
    ["Reviewer agreement", s.reviewer_agreement != null ? `${Math.round(s.reviewer_agreement * 100)}%` : "—"],
    ["Rejected (quote not in source)", s.rejected_for_unverified_quotes],
  ];

  return (
    <main className="page">
      <div className="tiles">
        {tiles.map(([label, value]) => (
          <div className="tile" key={label}>
            <div className="tile-value">{value}</div>
            <div className="muted small">{label}</div>
          </div>
        ))}
      </div>
      <p className="muted small">
        Reviewer agreement is confirmed ÷ (confirmed + dismissed). Keep it high before posting anything
        on Wikipedia: every dismissed finding is a false alarm the tool raised.
      </p>
      <div className="two-col">
        <section>
          <h3>Verdicts</h3>
          <table>
            <tbody>
              {Object.entries(s.verdicts).map(([k, v]) => (
                <tr key={k}>
                  <td>{k.replace(/_/g, " ")}</td>
                  <td className="num">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section>
          <h3>Findings by status</h3>
          <table>
            <tbody>
              {Object.entries(s.findings).map(([k, v]) => (
                <tr key={k}>
                  <td>{k.replace(/_/g, " ")}</td>
                  <td className="num">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </main>
  );
}
