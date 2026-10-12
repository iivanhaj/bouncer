"use client";

import { CheckIcon, CopyIcon, PlusIcon, TriangleAlertIcon } from "lucide-react";
import { useActionState, useState } from "react";
import { toast } from "sonner";

import { createKey, type CreateKeyState } from "@/app/actions";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function Field({ label, hint, ...input }: { label: string; hint?: string } & React.ComponentProps<typeof Input>) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={input.name}>{label}</Label>
      <Input id={input.name} {...input} />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function NewKey({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="grid gap-3">
      <div className="flex items-start gap-2 rounded-md border border-failed/40 bg-failed/10 p-3 text-sm">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-failed" />
        <span>Copy this key now. It is shown once; only its SHA-256 hash is stored.</span>
      </div>
      <div className="flex gap-2">
        <code className="flex-1 overflow-x-auto rounded-md border bg-muted px-3 py-2 font-mono text-xs whitespace-nowrap">
          {value}
        </code>
        <Button
          variant="outline"
          size="icon"
          aria-label="Copy key"
          onClick={async () => {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            toast.success("Key copied to the clipboard");
          }}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </Button>
      </div>
    </div>
  );
}

export function CreateKeyDialog() {
  const [open, setOpen] = useState(false);
  // Remounting the form (via key) clears the one-time key when the dialog is reopened.
  const [formKey, setFormKey] = useState(0);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setFormKey((k) => k + 1);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm">
          <PlusIcon /> New key
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <CreateKeyForm key={formKey} onDone={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  );
}

function CreateKeyForm({ onDone }: { onDone: () => void }) {
  const [state, formAction, pending] = useActionState<CreateKeyState, FormData>(createKey, {});

  if (state.created) {
    return (
      <>
        <DialogHeader>
          <DialogTitle>Key created for {state.created.team_name}</DialogTitle>
          <DialogDescription>Give it to the team over a secure channel.</DialogDescription>
        </DialogHeader>
        <NewKey value={state.created.key} />
        <DialogFooter>
          <Button onClick={onDone}>Done</Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <form action={formAction} className="grid gap-4">
      <DialogHeader>
        <DialogTitle>Create an API key</DialogTitle>
        <DialogDescription>One key per team or app. Limits apply per key.</DialogDescription>
      </DialogHeader>
      <Field label="Team name" name="team_name" required maxLength={100} placeholder="search-team" autoFocus />
      <div className="grid grid-cols-2 gap-4">
        <Field label="Requests per minute" name="rpm_limit" type="number" min={1} defaultValue={60} />
        <Field label="Max concurrent" name="max_concurrent" type="number" min={1} defaultValue={4} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <Field
          label="Monthly token budget"
          name="monthly_token_budget"
          type="number"
          min={1}
          placeholder="Unlimited"
          hint="Must cover 10× the in-flight reserve"
        />
        <Field label="Expires in days" name="expires_in_days" type="number" min={1} placeholder="Never" />
      </div>
      {state.error && <p className="text-sm text-destructive">{state.error}</p>}
      <DialogFooter>
        <Button type="submit" disabled={pending}>
          {pending ? "Creating…" : "Create key"}
        </Button>
      </DialogFooter>
    </form>
  );
}
