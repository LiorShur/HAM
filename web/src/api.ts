export type Run = {
  id: number;
  lang: string;
  title: string;
  requested_rev_id: number | null;
  rev_id: number | null;
  status: "queued" | "running" | "done" | "failed";
  error: string | null;
  stats: Record<string, number | string>;
  requested_by: string | null;
  created_at: string;
  finished_at: string | null;
  cost_usd: number;
  collection_id: number | null;
};

export type CollectionSummary = {
  id: number;
  topic: string;
  lang: string;
  created_by: string | null;
  created_at: string;
  article_count: number;
  selected_count: number;
};

export type CollectionArticle = {
  id: number;
  title: string;
  url: string;
  relevance: number;
  relevance_note: string;
  description: string;
  found_via: string;
  size_bytes: number | null;
  edit_protection: string | null;
  last_edited: string | null;
  selected: boolean;
  estimated_cost_usd: number;
  last_run: { id: number; status: Run["status"]; finished_at: string | null; cost_usd: number } | null;
  open_findings: number;
  open_uncited: number;
};

export type CollectionDetail = CollectionSummary & {
  cost_per_citation: number;
  cost_basis_runs: number;
  articles: CollectionArticle[];
};

export type FindingSummary = {
  id: number;
  category: "F2" | "uncited";
  severity: "none" | "minor" | "major" | null;
  needs_offline_check: boolean;
  article: string;
  lang: string;
  rev_id: number;
  section: string;
  claim: string;
  verdict: string;
  priority: "high" | "medium" | "low";
  status: "open" | "confirmed" | "dismissed" | "needs_research" | "superseded";
  confidence: number | null;
  created_at: string;
};

export type Quote = {
  kind: "supporting" | "contradicting";
  source_number: number | null;
  text: string;
  translation: string | null;
  verified: boolean;
  excerpt: string | null;
};

export type Review = { reviewer: string; decision: string; note: string; created_at: string };

export type SourceResult = {
  number: number;
  citation_id: number;
  citation_text: string;
  citation_urls: string[];
  citation_archive_url: string | null;
  citation_quote: string | null;
  verdict: string | null;
  explanation: string;
  source_url: string | null;
  source_via_archive: boolean;
  source_status: string | null;
  source_fetched_at: string | null;
};

export type FindingDetail = FindingSummary & {
  permalink: string;
  context_before: string;
  assessment: "source" | "claim" | "triage";
  details: {
    living_person?: boolean;
    in_lead?: boolean;
    citation_needed_tag?: boolean;
    covered_by?: number | null;
    covered_by_text?: string | null;
  };
  explanation: string;
  unsupported_parts: string[];
  quotes: Quote[];
  sources: SourceResult[];
  model: string | null;
  served_model: string | null;
  prompt_version: string | null;
  reviews: Review[];
};

export type Stats = {
  articles: number;
  verifications: number;
  verdicts: Record<string, number>;
  findings: Record<string, number>;
  reviewer_agreement: number | null;
  rejected_for_unverified_quotes: number;
};

const TOKEN_KEY = "wcm-token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives only in memory for this tab */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = (token: string) => ({
  me: () => request<{ reviewer: string }>(token, "/api/me"),
  runs: () => request<Run[]>(token, "/api/runs"),
  createRun: (body: { title?: string; rev_id?: number; lang: string }) =>
    request<Run>(token, "/api/runs", { method: "POST", body: JSON.stringify(body) }),
  findings: (status: string, category: string) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (category) params.set("category", category);
    return request<FindingSummary[]>(token, `/api/findings?${params}`);
  },
  finding: (id: number) => request<FindingDetail>(token, `/api/findings/${id}`),
  review: (id: number, decision: string, note: string) =>
    request<{ status: string }>(token, `/api/findings/${id}/reviews`, {
      method: "POST",
      body: JSON.stringify({ decision, note }),
    }),
  talkDraft: (id: number) => request<string>(token, `/api/findings/${id}/talk-draft`),
  stats: () => request<Stats>(token, "/api/stats"),
  collections: () => request<CollectionSummary[]>(token, "/api/collections"),
  collection: (id: number) => request<CollectionDetail>(token, `/api/collections/${id}`),
  createCollection: (topic: string, lang: string) =>
    request<CollectionDetail>(token, "/api/collections", {
      method: "POST",
      body: JSON.stringify({ topic, lang }),
    }),
  scanCollection: (id: number, titles: string[]) =>
    request<{ queued: number; run_ids: number[]; already_in_progress: string[] }>(
      token,
      `/api/collections/${id}/scan`,
      { method: "POST", body: JSON.stringify({ titles }) },
    ),
});

export type Api = ReturnType<typeof api>;
