# Karakos dashboard

Next.js 15 App Router dashboard for the Karakos agent system: roster, fleet,
chat, memory browser, history, costs and settings. It runs inside the package
container next to the agent-server and talks to it over HTTP
(`docs/package-backend-contract.md` in the repo root); a few read-only routes
open the agent-server's sqlite file directly.

Pages: `/` ops board, `/agents`, `/fleet`, `/chat`, `/memory`, `/history`,
`/costs`, `/settings`, `/login`. Auth is a password login that sets an
HMAC-signed `karakos_session` cookie.

## Develop

```
cd dashboard
npm ci
npm test            # vitest
npm run build       # next build
npm run dev         # next dev, needs the env below
```

## Environment variables

Set in `config/.env` (the container reads it) or `.env.local` for local dev.

| Variable | Required | Purpose |
| --- | --- | --- |
| `SESSION_SECRET` | yes | HMAC key signing the `karakos_session` cookie. Do not rotate casually: it logs everyone out. |
| `DASHBOARD_USER` | no (default `admin`) | Owner login name. |
| `DASHBOARD_PASSWORD` | yes | Owner password. Empty disables login. |
| `DASHBOARD_USERS` | no | Extra logins, `user:pass,user:pass`. |
| `DASHBOARD_PERMISSIONS` | no | JSON map of username to `{pages, agents, home}` allowlists. Unset: every account is unrestricted. |
| `DASHBOARD_PORT` | no (default 3000) | Port `next start` listens on. |
| `AGENT_SERVER_URL` | no (default `http://localhost:18791`) | Base URL of the agent-server. |
| `AGENT_SERVER_TOKEN` | yes | Bearer token for agent-server calls. |
| `AGENT_SERVER_DB_PATH` | no | Path to `agent-server.db` for the read-only routes. |
| `KARAKOS_REGISTRY_PATH` | no | Path to `config/agents.yaml` (read only) for the roster. |
| `KARAKOS_NAME` | no | Operator display name shown in the header. |
| `WORKSPACE_ROOT` | no (default `~/.karakos/workspace`) | Root for the dashboard's own database (`data/dashboard.db`). |
| `KARAKOS_COOKIE_SECURE` | no (default off) | Set `1` only when the dashboard is served over HTTPS. |
| `SESSION_MAX_AGE_SECONDS` | no (default 30 days) | Session lifetime. |
| `OWNER_NAME` | no | Author name attached to chat messages sent from the dashboard. |
| `VAPID_PUBLIC_KEY` | for web push | Public half of the VAPID keypair; served at `/api/push/vapid-public-key`. |
| `VAPID_PRIVATE_KEY` | for web push | Private half. Never expose to the client. |
| `VAPID_SUBJECT` | no | Contact `mailto:` URI sent with every push request. |

Generate a VAPID keypair with `npx web-push generate-vapid-keys`. Rotating it
invalidates every subscription; devices re-opt-in under Settings, Notifications.

## PWA

`app/manifest.ts` serves `/manifest.webmanifest`. `public/sw.js` is a small
hand-rolled service worker: cache-first for hashed static assets, network-only
for everything else (`/api/*` and page HTML are never cached). Bump
`CACHE_VERSION` in it on any change to cached assets. `public/offline.html` is
the fallback shown on a failed navigation.
