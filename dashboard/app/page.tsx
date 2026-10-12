import { api, type KeyUsage } from "@/lib/api";
import { date, ms, number, usd } from "@/lib/format";

import { revokeKey } from "./keys/actions";
import { CreateKeyForm } from "./keys/create-key-form";

// Always render on request: this page shows live data and must never be cached.
export const dynamic = "force-dynamic";

function BudgetBar({ budget, usage }: { budget: number | null; usage: KeyUsage | undefined }) {
  if (!budget) return <span className="muted">unlimited</span>;
  // No usage in the last 30 days means nothing spent this month either.
  if (!usage || usage.usable_budget == null) return <span className="muted">0 used of {number(budget)}</span>;
  const used = Math.min(1, usage.month_tokens / usage.usable_budget);
  return (
    <div
      className="budget"
      title={`${number(usage.month_tokens)} of ${number(usage.usable_budget)} usable tokens (budget ${number(budget)}; the rest is reserved for requests in progress)`}
    >
      <div className={`budget-fill ${used >= 0.9 ? "danger" : ""}`} style={{ width: `${used * 100}%` }} />
      <span>
        {number(usage.month_tokens)} / {number(usage.usable_budget)}
      </span>
    </div>
  );
}

function expiresSoon(expiresAt: string | null): boolean {
  return expiresAt != null && new Date(expiresAt).getTime() - Date.now() < 7 * 24 * 3600 * 1000;
}

export default async function KeysPage() {
  const [keys, usage] = await Promise.all([api.listKeys(), api.usage(30, "day")]);
  const usageByKey = new Map(usage.keys.map((u) => [u.key_id, u]));

  return (
    <>
      <h1>API keys</h1>
      <CreateKeyForm />
      <section className="card">
        <h2>All keys</h2>
        <table>
          <thead>
            <tr>
              <th>Team</th>
              <th>Key</th>
              <th>Limits</th>
              <th>This month&apos;s budget</th>
              <th className="num">Requests (30d)</th>
              <th className="num">Cost (30d)</th>
              <th className="num">p95 latency</th>
              <th>Expires</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  No keys yet.
                </td>
              </tr>
            )}
            {keys.map((key) => {
              const u = usageByKey.get(key.id);
              return (
                <tr key={key.id} className={key.revoked_at ? "revoked" : ""}>
                  <td>{key.team_name}</td>
                  <td>
                    <code>{key.key_prefix}…</code>
                  </td>
                  <td>
                    {key.rpm_limit}/min, {key.max_concurrent} at once
                  </td>
                  <td>
                    <BudgetBar budget={key.monthly_token_budget} usage={u} />
                  </td>
                  <td className="num">{number(u?.requests ?? 0)}</td>
                  <td className="num">{usd(u?.cost_usd ?? 0)}</td>
                  <td className="num">{ms(u?.p95_latency_ms)}</td>
                  <td className={expiresSoon(key.expires_at) && !key.revoked_at ? "warn" : ""}>
                    {key.expires_at ? date(key.expires_at) : "never"}
                  </td>
                  <td>
                    {key.revoked_at ? (
                      <span className="muted">revoked</span>
                    ) : (
                      <form action={revokeKey}>
                        <input type="hidden" name="id" value={key.id} />
                        <button className="danger-button" type="submit">
                          Revoke
                        </button>
                      </form>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}
