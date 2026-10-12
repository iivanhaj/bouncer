"use client";

import { useActionState } from "react";

import { createKey, type CreateKeyState } from "./actions";

export function CreateKeyForm() {
  const [state, formAction, pending] = useActionState<CreateKeyState, FormData>(createKey, {});

  return (
    <section className="card">
      <h2>Create a key</h2>
      <form action={formAction} className="form-grid">
        <label>
          Team name
          <input name="team_name" required maxLength={100} placeholder="search-team" />
        </label>
        <label>
          Requests / minute
          <input name="rpm_limit" type="number" min={1} defaultValue={60} />
        </label>
        <label>
          Max concurrent
          <input name="max_concurrent" type="number" min={1} defaultValue={4} />
        </label>
        <label>
          Monthly token budget
          <input name="monthly_token_budget" type="number" min={1} placeholder="unlimited" />
        </label>
        <label>
          Expires in days
          <input name="expires_in_days" type="number" min={1} placeholder="never" />
        </label>
        <button type="submit" disabled={pending}>
          {pending ? "Creating…" : "Create key"}
        </button>
      </form>

      {state.error && <p className="error">{state.error}</p>}
      {state.created && (
        <div className="new-key">
          <p>
            <strong>Copy this key now.</strong> It is shown once and only its hash is stored.
          </p>
          <code>{state.created.key}</code>
        </div>
      )}
    </section>
  );
}
