import { api } from "@/lib/api";
import { date } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function BlockedPage() {
  const blocked = await api.blocked(200);

  return (
    <>
      <h1>Blocked requests</h1>
      <p className="muted">
        Prompts are never stored, so only the matched rules and score are shown. &ldquo;Would block&rdquo; rows come from
        monitor mode: they matched, but were let through.
      </p>
      <section className="card">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Team</th>
              <th>Model</th>
              <th>Outcome</th>
              <th className="num">Score</th>
              <th>Rules</th>
              <th>Request id</th>
            </tr>
          </thead>
          <tbody>
            {blocked.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  Nothing blocked yet.
                </td>
              </tr>
            )}
            {blocked.map((b) => (
              <tr key={b.id}>
                <td>{date(b.created_at)}</td>
                <td>
                  {b.team_name} <code>{b.key_prefix}…</code>
                </td>
                <td>{b.model}</td>
                <td>{b.blocked ? <span className="pill danger">blocked</span> : <span className="pill warn">would block</span>}</td>
                <td className="num">{b.score.toFixed(2)}</td>
                <td>
                  {b.rules.map((rule) => (
                    <code key={rule} className="rule">
                      {rule}
                    </code>
                  ))}
                </td>
                <td>
                  <code className="muted">{b.id}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
