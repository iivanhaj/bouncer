"use server";

// Server actions: these run on the dashboard's server, never in the browser, so they can use
// the admin token. The browser only ever sees their results.
import { revalidatePath } from "next/cache";

import { api, ApiError, type CreatedKey } from "@/lib/api";

export type CreateKeyState = { created?: CreatedKey; error?: string };

function optionalInt(value: FormDataEntryValue | null): number | undefined {
  const text = String(value ?? "").trim();
  return text === "" ? undefined : Number(text);
}

export async function createKey(_previous: CreateKeyState, form: FormData): Promise<CreateKeyState> {
  const body = {
    team_name: String(form.get("team_name") ?? "").trim(),
    rpm_limit: optionalInt(form.get("rpm_limit")),
    max_concurrent: optionalInt(form.get("max_concurrent")),
    monthly_token_budget: optionalInt(form.get("monthly_token_budget")),
    expires_in_days: optionalInt(form.get("expires_in_days")),
  };
  try {
    const created = await api.createKey(Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined)));
    revalidatePath("/", "layout");
    return { created };
  } catch (error) {
    return { error: error instanceof ApiError ? error.message : "Could not create the key" };
  }
}

export async function revokeKey(id: string): Promise<{ error?: string }> {
  try {
    await api.revokeKey(id);
    revalidatePath("/", "layout");
    return {};
  } catch (error) {
    return { error: error instanceof ApiError ? error.message : "Could not revoke the key" };
  }
}
