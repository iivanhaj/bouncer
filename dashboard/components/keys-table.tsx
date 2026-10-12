import { RevokeKeyButton } from "@/components/revoke-key-button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { ApiKey, KeyUsage } from "@/lib/api";
import { date, ms, number, usd } from "@/lib/format";

const WEEK_MS = 7 * 24 * 3600 * 1000;

// Server component: it renders once per request, so reading the clock here is intended.
/* eslint-disable react-hooks/purity */
function Status({ apiKey }: { apiKey: ApiKey }) {
  if (apiKey.revoked_at) return <Badge variant="secondary">Revoked</Badge>;
  if (apiKey.expires_at && new Date(apiKey.expires_at).getTime() < Date.now())
    return <Badge variant="secondary">Expired</Badge>;
  if (apiKey.expires_at && new Date(apiKey.expires_at).getTime() - Date.now() < WEEK_MS)
    return (
      <Badge variant="outline" className="border-failed/50 text-failed">
        Expires soon
      </Badge>
    );
  return (
    <Badge variant="outline" className="border-served/50 text-served">
      Active
    </Badge>
  );
}
/* eslint-enable react-hooks/purity */

function Budget({ budget, usage }: { budget: number | null; usage: KeyUsage | undefined }) {
  if (!budget) return <span className="text-muted-foreground">Unlimited</span>;
  const spent = usage?.month_tokens ?? 0;
  const usable = usage?.usable_budget;
  if (usable == null) return <span className="text-muted-foreground">0 of {number(budget)}</span>;
  const percent = Math.min(100, (spent / usable) * 100);
  return (
    <div className="grid w-40 gap-1" title={`Budget ${number(budget)}; the rest is reserved for requests in progress`}>
      <Progress value={percent} className={percent >= 90 ? "[&>div]:bg-blocked" : "[&>div]:bg-served"} />
      <span className="text-xs text-muted-foreground tabular-nums">
        {number(spent)} / {number(usable)} usable
      </span>
    </div>
  );
}

export function KeysTable({ keys, usage, compact = false }: { keys: ApiKey[]; usage: KeyUsage[]; compact?: boolean }) {
  const usageByKey = new Map(usage.map((u) => [u.key_id, u]));

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Team</TableHead>
          <TableHead>Status</TableHead>
          {!compact && <TableHead>Limits</TableHead>}
          <TableHead>This month&apos;s budget</TableHead>
          <TableHead className="text-right">Requests</TableHead>
          <TableHead className="text-right">Cost</TableHead>
          <TableHead className="text-right">p95</TableHead>
          {!compact && <TableHead>Expires</TableHead>}
          {!compact && <TableHead />}
        </TableRow>
      </TableHeader>
      <TableBody>
        {keys.length === 0 && (
          <TableRow>
            <TableCell colSpan={9} className="h-24 text-center text-muted-foreground">
              No keys yet. Create one to start sending requests.
            </TableCell>
          </TableRow>
        )}
        {keys.map((key) => {
          const u = usageByKey.get(key.id);
          return (
            <TableRow key={key.id} className={key.revoked_at ? "opacity-60" : undefined}>
              <TableCell>
                <div className="font-medium">{key.team_name}</div>
                <code className="font-mono text-xs text-muted-foreground">{key.key_prefix}…</code>
              </TableCell>
              <TableCell>
                <Status apiKey={key} />
              </TableCell>
              {!compact && (
                <TableCell className="text-muted-foreground">
                  {key.rpm_limit} per minute, {key.max_concurrent} at once
                </TableCell>
              )}
              <TableCell>
                <Budget budget={key.monthly_token_budget} usage={u} />
              </TableCell>
              <TableCell className="text-right tabular-nums">{number(u?.requests ?? 0)}</TableCell>
              <TableCell className="text-right tabular-nums">{usd(u?.cost_usd ?? 0)}</TableCell>
              <TableCell className="text-right tabular-nums">{ms(u?.p95_latency_ms)}</TableCell>
              {!compact && (
                <TableCell className="text-muted-foreground">{key.expires_at ? date(key.expires_at) : "Never"}</TableCell>
              )}
              {!compact && (
                <TableCell className="text-right">
                  {!key.revoked_at && <RevokeKeyButton id={key.id} teamName={key.team_name} prefix={key.key_prefix} />}
                </TableCell>
              )}
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
