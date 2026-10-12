# Bouncer dashboard

Admin console for the Bouncer gateway: an overview of traffic at the door, API keys, usage and
blocked requests. Next.js 16 with [shadcn/ui](https://ui.shadcn.com) components (started from
its `dashboard-01` block) and Tailwind CSS 4.

See the main [README](../README.md) for how it fits in, and [DESIGN.md](../DESIGN.md) §7 for why
it is published on localhost only.

## Run

```bash
npm install
BOUNCER_ADMIN_TOKEN=<admin token> GATEWAY_URL=http://localhost:8000 npm run dev
```

Then open http://127.0.0.1:3000.

## Design

- **One bold element:** the overview's door tally, with requests split into served, blocked and
  failed. Everything else is quiet: hairline rules, no shadows or gradients.
- **Colour carries meaning:** `served` (teal), `blocked` (velvet-rope burgundy, also the brand)
  and `failed` (amber) are theme tokens in `app/globals.css`, used for the tally, badges and
  budget bars. Chart series for teams never use the burgundy.
- **One typeface:** Archivo, using its width axis (`.font-wide`) for the wordmark and headline
  numbers. Monospace only for real code values: key prefixes, request ids, rule ids.

## How the admin token stays private

- `lib/api.ts` is the only code that uses the token, and it imports `server-only`: the build
  fails if it is ever imported into code that runs in the browser.
- Pages are server components; create and revoke are server actions. The browser only receives
  rendered HTML and action results.
