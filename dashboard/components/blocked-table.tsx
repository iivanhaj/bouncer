import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { BlockedRequest } from "@/lib/api";
import { date } from "@/lib/format";

export function BlockedTable({ rows, compact = false }: { rows: BlockedRequest[]; compact?: boolean }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>When</TableHead>
          <TableHead>Team</TableHead>
          <TableHead>Outcome</TableHead>
          <TableHead className="text-right">Score</TableHead>
          <TableHead>Matched rules</TableHead>
          {!compact && <TableHead>Request id</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.length === 0 && (
          <TableRow>
            <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
              Nothing blocked yet.
            </TableCell>
          </TableRow>
        )}
        {rows.map((row) => (
          <TableRow key={row.id}>
            <TableCell className="whitespace-nowrap text-muted-foreground">{date(row.created_at)}</TableCell>
            <TableCell>
              <div className="font-medium">{row.team_name}</div>
              <div className="text-xs text-muted-foreground">{row.model}</div>
            </TableCell>
            <TableCell>
              {row.blocked ? (
                <Badge className="bg-blocked text-white">Blocked</Badge>
              ) : (
                <Badge variant="outline" className="border-failed/50 text-failed">
                  Would block
                </Badge>
              )}
            </TableCell>
            <TableCell className="text-right tabular-nums">{row.score.toFixed(2)}</TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                {row.rules.map((rule) => (
                  <Badge key={rule} variant="secondary" className="font-mono text-[11px] font-normal">
                    {rule}
                  </Badge>
                ))}
              </div>
            </TableCell>
            {!compact && (
              <TableCell>
                <code className="font-mono text-xs text-muted-foreground">{row.id}</code>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
