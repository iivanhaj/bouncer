import { BlockedTable } from "@/components/blocked-table";
import { SiteHeader } from "@/components/site-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function BlockedPage() {
  const rows = await api.blocked(200);

  return (
    <>
      <SiteHeader title="Blocked requests" />
      <div className="@container/main flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
        <Card>
          <CardHeader>
            <CardTitle>Prompt-injection detection</CardTitle>
            <CardDescription>
              Prompts are never stored, so only matched rules and scores are shown. &ldquo;Would block&rdquo; rows come
              from monitor mode: they matched, but were let through.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <BlockedTable rows={rows} />
          </CardContent>
        </Card>
      </div>
    </>
  );
}
