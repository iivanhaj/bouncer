import { CreateKeyDialog } from "@/components/create-key-dialog";
import { KeysTable } from "@/components/keys-table";
import { SiteHeader } from "@/components/site-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function KeysPage() {
  const [keys, usage] = await Promise.all([api.listKeys(), api.usage(30, "day")]);

  return (
    <>
      <SiteHeader title="API keys">
        <CreateKeyDialog />
      </SiteHeader>
      <div className="@container/main flex flex-1 flex-col gap-4 p-4 md:gap-6 md:p-6">
        <Card>
          <CardHeader>
            <CardTitle>All keys</CardTitle>
            <CardDescription>
              Keys are stored as SHA-256 hashes; only the prefix is shown. Requests, cost and p95 cover the last 30
              days.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <KeysTable keys={keys} usage={usage.keys} />
          </CardContent>
        </Card>
      </div>
    </>
  );
}
