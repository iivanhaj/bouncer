import Link from "next/link";

import { BlockedTable } from "@/components/blocked-table";
import { DoorTally, MetricRow } from "@/components/door-tally";
import { KeysTable } from "@/components/keys-table";
import { RangeToggle } from "@/components/range-toggle";
import { SiteHeader } from "@/components/site-header";
import { TokensChart } from "@/components/tokens-chart";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { parseDays } from "@/lib/range";

// Live data: render on every request, never cache.
export const dynamic = "force-dynamic";

function Section({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="grid content-start gap-3">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
        {action}
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">{children}</div>
    </section>
  );
}

export default async function OverviewPage({ searchParams }: PageProps<"/">) {
  const days = parseDays((await searchParams).days);
  const [usage, keys, blocked] = await Promise.all([api.usage(days, "day"), api.listKeys(), api.blocked(5)]);
  const activeKeys = keys.filter((k) => !k.revoked_at);
  // Busiest first by requests in the window; keys with no traffic go last (not first: a missing
  // key would get index -1 from indexOf and sort ahead of everything).
  const requestsByKey = new Map(usage.keys.map((u) => [u.key_id, u.requests]));
  const topKeys = [...activeKeys]
    .sort((a, b) => (requestsByKey.get(b.id) ?? 0) - (requestsByKey.get(a.id) ?? 0))
    .slice(0, 5);

  return (
    <>
      <SiteHeader title="Overview">
        <RangeToggle days={days} />
      </SiteHeader>
      <div className="@container/main flex flex-1 flex-col gap-8 p-4 md:p-8">
        <DoorTally totals={usage.totals} days={days} />
        <MetricRow totals={usage.totals} />
        <TokensChart series={usage.series} teams={usage.keys} since={usage.since} days={days} />
        <div className="grid gap-8 @5xl/main:grid-cols-2">
          <Section
            title="Busiest keys"
            description={`${activeKeys.length} of ${keys.length} keys active`}
            action={
              <Button asChild variant="outline" size="sm">
                <Link href="/keys">Manage keys</Link>
              </Button>
            }
          >
            <KeysTable keys={topKeys} usage={usage.keys} compact />
          </Section>
          <Section
            title="Recently blocked"
            description="Prompt-injection attempts stopped at the door"
            action={
              <Button asChild variant="outline" size="sm">
                <Link href="/blocked">See all</Link>
              </Button>
            }
          >
            <BlockedTable rows={blocked} compact />
          </Section>
        </div>
      </div>
    </>
  );
}
