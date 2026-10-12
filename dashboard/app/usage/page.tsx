import { RangeToggle } from "@/components/range-toggle";
import { SiteHeader } from "@/components/site-header";
import { TokensChart } from "@/components/tokens-chart";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { ms, number, usd } from "@/lib/format";
import { parseDays } from "@/lib/range";

export const dynamic = "force-dynamic";

export default async function UsagePage({ searchParams }: PageProps<"/usage">) {
  const days = parseDays((await searchParams).days);
  const report = await api.usage(days, "day");

  return (
    <>
      <SiteHeader title="Usage">
        <RangeToggle days={days} />
      </SiteHeader>
      <div className="@container/main flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
        <TokensChart series={report.series} teams={report.keys} since={report.since} days={days} />
        <Card>
          <CardHeader>
            <CardTitle>By key</CardTitle>
            <CardDescription>
              Latency is the whole request, gateway to last byte. Time to first token is for streamed responses.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Team</TableHead>
                  <TableHead className="text-right">Requests</TableHead>
                  <TableHead className="text-right">Prompt</TableHead>
                  <TableHead className="text-right">Completion</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead className="text-right">Avg</TableHead>
                  <TableHead className="text-right">p95</TableHead>
                  <TableHead className="text-right">First token</TableHead>
                  <TableHead className="text-right">Errors</TableHead>
                  <TableHead className="text-right">Blocked</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.keys.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={10} className="h-24 text-center text-muted-foreground">
                      No requests in this period.
                    </TableCell>
                  </TableRow>
                )}
                {report.keys.map((k) => (
                  <TableRow key={k.key_id} className={k.revoked ? "opacity-60" : undefined}>
                    <TableCell>
                      <div className="font-medium">{k.team_name}</div>
                      <code className="font-mono text-xs text-muted-foreground">{k.key_prefix}…</code>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{number(k.requests)}</TableCell>
                    <TableCell className="text-right tabular-nums">{number(k.prompt_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{number(k.completion_tokens)}</TableCell>
                    <TableCell className="text-right tabular-nums">{usd(k.cost_usd)}</TableCell>
                    <TableCell className="text-right tabular-nums">{ms(k.avg_latency_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">{ms(k.p95_latency_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">{ms(k.avg_ttft_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {k.errors > 0 ? <Badge variant="outline" className="border-failed/50 text-failed">{number(k.errors)}</Badge> : "0"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {k.blocked > 0 ? <Badge className="bg-blocked text-white">{number(k.blocked)}</Badge> : "0"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
