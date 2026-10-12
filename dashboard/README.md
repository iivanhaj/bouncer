# Bouncer dashboard

Admin dashboard for the Bouncer gateway: keys, usage and blocked requests. See the main
[README](../README.md) for how it fits in and [DESIGN.md](../DESIGN.md) §7 for why it is
published on localhost only.

## Run

```bash
npm install
BOUNCER_ADMIN_TOKEN=<admin token> GATEWAY_URL=http://localhost:8000 npm run dev
```

Then open http://127.0.0.1:3000.

## How the admin token stays private

- `lib/api.ts` is the only code that uses the token, and it imports `server-only`: the build
  fails if it is ever imported into code that runs in the browser.
- Pages are server components and create/revoke are server actions, so the browser only ever
  receives rendered HTML and action results.
