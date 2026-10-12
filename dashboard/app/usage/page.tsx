import { api, type UsagePoint } from "@/lib/api";
import { ms, number, usd } from "@/lib/format";

export const dynamic = "force-dynamic";

const COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#dc2626", "#0891b2"];

/** Stacked daily token bars per key, drawn as plain SVG (no chart library needed). */
function TokenChart({ series, keyOrder }: { series: UsagePoint[]; keyOrder: string[] }) {
  const days = [...new Set(series.map((p) => p.bucket))].sort();
  if (days.length === 0) return <p className="muted">No requests in this period.</p>;
  const totals = days.map((d) => series.filter((p) => p.bucket === d).reduce((sum, p) => sum + p.tokens, 0));
  const max = Math.max(1, ...totals);
  const width = 720;
  const height = 220;
  // Capped, so a period with one or two days doesn't draw one giant block.
  const barWidth = Math.min(48, Math.max(4, width / days.length - 6));

  return (
    <svg viewBox={`0 0 ${width} ${height + 24}`} className="chart" role="img" aria-label="Tokens per day by key">
      {days.map((day, i) => {
        let y = height;
        const x = i * (width / days.length) + 3;
        return (
          <g key={day}>
            {keyOrder.map((keyId, k) => {
              const point = series.find((p) => p.bucket === day && p.key_id === keyId);
              if (!point || point.tokens === 0) return null;
              const h = (point.tokens / max) * (height - 10);
              y -= h;
              return (
                <rect key={keyId} x={x} y={y} width={barWidth} height={h} fill={COLORS[k % COLORS.length]}>
                  <title>{`${day.slice(0, 10)}: ${number(point.tokens)} tokens, ${point.requests} requests`}</title>
                </rect>
              );
            })}
            {(days.length <= 14 || i % Math.ceil(days.length / 14) === 0) && (
              <text x={x + barWidth / 2} y={height + 16} textAnchor="middle" className="axis">
                {day.slice(5, 10)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export default async function UsagePage({ searchParams }: { searchParams: Promise<{ days?: string }> }) {
  const days = Math.min(365, Math.max(1, Number((await searchParams).days) || 30));
  const report = await api.usage(days, "day");
  const keyOrder = report.keys.map((k) => k.key_id);

  return (
    <>
      <h1>Usage</h1>
      <p className="muted">
        Last {days} days.{" "}
        {[7, 30, 90].map((d) => (
          <a key={d} href={`/usage?days=${d}`} className={d === days ? "active-link" : ""}>
            {d}d{" "}
          </a>
        ))}
      </p>

      <section className="card">
        <h2>Tokens per day</h2>
        <TokenChart series={report.series} keyOrder={keyOrder} />
        <div className="legend">
          {report.keys.map((k, i) => (
            <span key={k.key_id}>
              <i style={{ background: COLORS[i % COLORS.length] }} /> {k.team_name}
            </span>
          ))}
        </div>
      </section>

      <section className="card">
        <h2>Per key</h2>
        <table>
          <thead>
            <tr>
              <th>Team</th>
              <th className="num">Requests</th>
              <th className="num">Prompt tokens</th>
              <th className="num">Completion tokens</th>
              <th className="num">Cost</th>
              <th className="num">Avg latency</th>
              <th className="num">p95 latency</th>
              <th className="num">Avg first token</th>
              <th className="num">Errors</th>
              <th className="num">Blocked</th>
            </tr>
          </thead>
          <tbody>
            {report.keys.map((k) => (
              <tr key={k.key_id} className={k.revoked ? "revoked" : ""}>
                <td>
                  {k.team_name} <code>{k.key_prefix}…</code>
                </td>
                <td className="num">{number(k.requests)}</td>
                <td className="num">{number(k.prompt_tokens)}</td>
                <td className="num">{number(k.completion_tokens)}</td>
                <td className="num">{usd(k.cost_usd)}</td>
                <td className="num">{ms(k.avg_latency_ms)}</td>
                <td className="num">{ms(k.p95_latency_ms)}</td>
                <td className="num">{ms(k.avg_ttft_ms)}</td>
                <td className="num">{number(k.errors)}</td>
                <td className="num">{number(k.blocked)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
