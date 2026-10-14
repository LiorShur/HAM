import { useEffect, useMemo, useState } from "react";
import { api, getToken, setToken } from "./api";
import ReviewQueue from "./ReviewQueue";
import Runs from "./Runs";
import StatsView from "./Stats";

type Tab = "queue" | "runs" | "stats";

export default function App() {
  const [token, setTokenState] = useState(getToken());
  const [reviewer, setReviewer] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("queue");
  const [loginError, setLoginError] = useState("");
  const client = useMemo(() => api(token), [token]);

  useEffect(() => {
    if (!token) return;
    client
      .me()
      .then((r) => setReviewer(r.reviewer))
      .catch(() => {
        setReviewer(null);
        setLoginError("That token was not accepted.");
        setToken("");
        setTokenState("");
      });
  }, [client, token]);

  if (!token || !reviewer) {
    return (
      <main className="login">
        <h1>Citation Monitor</h1>
        <p className="muted">Checks whether Wikipedia citations support the text they're attached to. Every finding is reviewed by a person before anything is said on Wikipedia.</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const value = new FormData(e.currentTarget).get("token") as string;
            setLoginError("");
            setToken(value.trim());
            setTokenState(value.trim());
          }}
        >
          <label>
            Reviewer token
            <input name="token" type="password" autoComplete="current-password" required />
          </label>
          <button type="submit">Sign in</button>
          {loginError && <p className="error">{loginError}</p>}
        </form>
      </main>
    );
  }

  return (
    <>
      <header className="topbar">
        <strong>Citation Monitor</strong>
        <nav>
          {(["queue", "runs", "stats"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
              {{ queue: "Review queue", runs: "Runs", stats: "Stats" }[t]}
            </button>
          ))}
        </nav>
        <span className="muted">
          {reviewer} ·{" "}
          <button
            className="link"
            onClick={() => {
              setToken("");
              setTokenState("");
              setReviewer(null);
            }}
          >
            sign out
          </button>
        </span>
      </header>
      {tab === "queue" && <ReviewQueue client={client} />}
      {tab === "runs" && <Runs client={client} />}
      {tab === "stats" && <StatsView client={client} />}
    </>
  );
}
