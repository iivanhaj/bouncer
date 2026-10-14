// Server-only client for the gateway's admin API.
//
// "server-only" makes the build fail if this file is ever imported into code that runs in the
// browser, so the admin token can't leak to the client by accident (DESIGN.md §7).
import "server-only";

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8000";

function adminToken(): string {
  const token = process.env.MODELGUARD_ADMIN_TOKEN;
  if (!token) throw new Error("MODELGUARD_ADMIN_TOKEN is not set for the dashboard");
  return token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${GATEWAY_URL}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${adminToken()}`, "Content-Type": "application/json", ...init.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, body?.error?.message ?? `Gateway returned ${response.status}`);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export type ApiKey = {
  id: string;
  team_name: string;
  key_prefix: string;
  rpm_limit: number;
  max_concurrent: number;
  monthly_token_budget: number | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
};

export type CreatedKey = ApiKey & { key: string };

export type UsageTotals = {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: string;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  avg_ttft_ms: number | null;
  errors: number;
  blocked: number;
};

export type KeyUsage = UsageTotals & {
  key_id: string;
  team_name: string;
  key_prefix: string;
  revoked: boolean;
  month_tokens: number;
  monthly_token_budget: number | null;
  usable_budget: number | null;
};

export type UsagePoint = {
  key_id: string;
  bucket: string;
  requests: number;
  tokens: number;
  cost_usd: string;
  p95_latency_ms: number | null;
};

export type UsageReport = {
  since: string;
  bucket: "hour" | "day";
  totals: UsageTotals;
  keys: KeyUsage[];
  series: UsagePoint[];
};

export type BlockedRequest = {
  id: string;
  created_at: string;
  key_id: string;
  team_name: string;
  key_prefix: string;
  model: string;
  rules: string[];
  score: number;
  blocked: boolean;
};

export const api = {
  listKeys: () => request<ApiKey[]>("/admin/keys"),
  createKey: (body: Record<string, unknown>) =>
    request<CreatedKey>("/admin/keys", { method: "POST", body: JSON.stringify(body) }),
  revokeKey: (id: string) => request<void>(`/admin/keys/${encodeURIComponent(id)}`, { method: "DELETE" }),
  usage: (days: number, bucket: "hour" | "day") => request<UsageReport>(`/admin/usage?days=${days}&bucket=${bucket}`),
  blocked: (limit = 100) => request<BlockedRequest[]>(`/admin/blocked?limit=${limit}`),
};
