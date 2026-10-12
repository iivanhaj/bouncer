import type { UsageTotals } from "@/lib/api";
import { ms, number, usd } from "@/lib/format";

/**
 * The page's one bold element: how many requests came to the door, and how they split into
 * served, blocked (prompt injection) and failed (upstream errors or limits).
 */
export function DoorTally({ totals, days }: { totals: UsageTotals; days: number }) {
  const served = Math.max(0, totals.requests - totals.blocked - totals.errors);
  const parts = [
    { label: "served", value: served, color: "bg-served", text: "text-served" },
    { label: "blocked", value: totals.blocked, color: "bg-blocked", text: "text-blocked" },
    { label: "failed", value: totals.errors, color: "bg-failed", text: "text-failed" },
  ];

  return (
    <section aria-labelledby="tally-heading" className="grid gap-4">
      <h2 id="tally-heading" className="flex flex-wrap items-baseline gap-x-3">
        <span className="font-wide text-5xl font-bold tracking-tight tabular-nums">{number(totals.requests)}</span>
        <span className="text-lg text-muted-foreground">
          requests in the last {days} days
        </span>
      </h2>

      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={parts.map((p) => `${p.value} ${p.label}`).join(", ")}
      >
        {totals.requests > 0 &&
          parts.map(
            (p) =>
              p.value > 0 && (
                <div key={p.label} className={p.color} style={{ width: `${(p.value / totals.requests) * 100}%` }} />
              ),
          )}
      </div>

      <dl className="flex flex-wrap gap-x-8 gap-y-2">
        {parts.map((p) => (
          <div key={p.label} className="flex items-baseline gap-2">
            <dt className="sr-only">{p.label}</dt>
            <dd className={`font-wide text-2xl font-semibold tabular-nums ${p.text}`}>{number(p.value)}</dd>
            <span className="text-sm text-muted-foreground">{p.label}</span>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** Secondary numbers in one hairline-divided row, quieter than the tally. */
export function MetricRow({ totals }: { totals: UsageTotals }) {
  const metrics = [
    {
      label: "Tokens",
      value: number(totals.prompt_tokens + totals.completion_tokens),
      detail: `${number(totals.prompt_tokens)} prompt, ${number(totals.completion_tokens)} completion`,
    },
    { label: "Cost", value: usd(totals.cost_usd), detail: "at models.yaml prices" },
    { label: "p95 latency", value: ms(totals.p95_latency_ms), detail: `average ${ms(totals.avg_latency_ms)}` },
    { label: "First token", value: ms(totals.avg_ttft_ms), detail: "average, streamed responses" },
  ];

  return (
    <dl className="grid grid-cols-2 border-y @3xl/main:grid-cols-4">
      {metrics.map((m, i) => (
        <div
          key={m.label}
          className={`grid gap-0.5 px-1 py-4 @3xl/main:px-5 ${i > 0 ? "@3xl/main:border-l" : ""} ${i % 2 === 1 ? "border-l @3xl/main:border-l" : ""} ${i >= 2 ? "border-t @3xl/main:border-t-0" : ""}`}
        >
          <dt className="text-sm text-muted-foreground">{m.label}</dt>
          <dd className="text-xl font-semibold tabular-nums">{m.value}</dd>
          <dd className="text-xs text-muted-foreground">{m.detail}</dd>
        </div>
      ))}
    </dl>
  );
}
