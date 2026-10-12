"use client";

import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type ChartConfig,
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import type { UsagePoint } from "@/lib/api";

const MAX_SERIES = 5;

/** Daily tokens, stacked by team: the top teams by volume, the rest grouped as "Other". */
export function TokensChart({
  series,
  teams,
  since,
  days,
}: {
  series: UsagePoint[];
  teams: { key_id: string; team_name: string }[];
  since: string;
  days: number;
}) {
  const shown = teams.slice(0, MAX_SERIES);
  const seriesKey = new Map(shown.map((t, i) => [t.key_id, `s${i}`]));
  const config: ChartConfig = Object.fromEntries(
    shown.map((t, i) => [`s${i}`, { label: t.team_name, color: `var(--chart-${i + 1})` }]),
  );
  const hasOther = teams.length > MAX_SERIES;
  if (hasOther) config.other = { label: "Other", color: "var(--muted-foreground)" };

  // One row per day, with every day in the window present (zero when quiet). Built from the
  // API's own "since", not the clock, so server and browser renders always agree.
  const rows = new Map<string, Record<string, number | string>>();
  for (let i = 1; i <= days; i++) {
    const date = new Date(Date.parse(since) + i * 86_400_000).toISOString().slice(0, 10);
    rows.set(date, { date, ...Object.fromEntries(Object.keys(config).map((k) => [k, 0])) });
  }
  for (const point of series) {
    const row = rows.get(point.bucket.slice(0, 10));
    if (!row) continue;
    const key = seriesKey.get(point.key_id) ?? "other";
    row[key] = Number(row[key] ?? 0) + point.tokens;
  }
  const data = [...rows.values()];

  return (
    <Card className="@container/card">
      <CardHeader>
        <CardTitle>Tokens per day</CardTitle>
        <CardDescription>Prompt and completion tokens through the gateway, by team</CardDescription>
      </CardHeader>
      <CardContent className="px-2 pt-2 sm:px-6">
        {teams.length === 0 ? (
          <div className="flex h-[250px] items-center justify-center text-sm text-muted-foreground">
            No requests in this period.
          </div>
        ) : (
          <ChartContainer config={config} className="aspect-auto h-[250px] w-full">
            <AreaChart data={data} margin={{ left: 0, right: 8 }}>
              <defs>
                {Object.keys(config).map((key) => (
                  <linearGradient key={key} id={`fill-${key}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={`var(--color-${key})`} stopOpacity={0.8} />
                    <stop offset="95%" stopColor={`var(--color-${key})`} stopOpacity={0.1} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid vertical={false} />
              <XAxis
                dataKey="date"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={32}
                tickFormatter={(value: string) =>
                  new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })
                }
              />
              <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={(v: number) => v.toLocaleString("en-US")} />
              <ChartTooltip
                cursor={false}
                content={
                  <ChartTooltipContent
                    indicator="dot"
                    labelFormatter={(value) =>
                      new Date(String(value)).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })
                    }
                  />
                }
              />
              {Object.keys(config).map((key) => (
                <Area
                  key={key}
                  dataKey={key}
                  type="monotone"
                  stackId="tokens"
                  fill={`url(#fill-${key})`}
                  stroke={`var(--color-${key})`}
                />
              ))}
              <ChartLegend content={<ChartLegendContent />} />
            </AreaChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}
