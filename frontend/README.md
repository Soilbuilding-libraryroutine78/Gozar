# Gozar Web Console

React + TypeScript (strict) single-page admin console for Gozar, built with Vite.
It talks to the backend admin API mounted at `/api` and provides operator login
plus (in later tasks) account, token, fallback-chain, trace, and analytics views.

## Stack

- React 18 + React Router 6
- TypeScript in strict mode (`tsconfig.json`: `strict`, `noImplicitAny`,
  `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, and more)
- Vite 5
- Dependency versions are pinned (no `^`/`~` ranges)

## Project layout

```
src/
  routes.ts            single source of truth for client route paths
  api/
    config.ts          API base URL + every endpoint path
    types.ts           typed request/response interfaces (no `any` at boundaries)
    errors.ts          ApiError + error-envelope parsing
    client.ts          typed fetch wrapper (bearer auth, silent refresh, retry)
    auth.ts            operator login / refresh / bootstrap calls
  auth/
    AuthContext.tsx    session state; wires the client auth hooks
    session-storage.ts persistence of the session token bundle
  components/          icons (outline SVG only), Spinner, ProtectedRoute
  pages/               LoginPage, DashboardPage, NotFoundPage
```

## Local development

```bash
npm install
npm run dev        # http://localhost:5173 (proxies /api and /v1 to the backend)
```

Configure the backend origin by copying `.env.example` to `.env`. In development
the Vite proxy forwards `/api` and `/v1` to `VITE_DEV_PROXY_TARGET`.

## Build / type-check

```bash
npm run build      # tsc --noEmit (strict) + vite production build
npm run typecheck  # strict type-check only
```

## Docker

```bash
# Production image (nginx serving the static bundle, port 80):
docker build --target runtime -t gozar-frontend .

# Via compose (frontend profile):
docker compose --profile frontend up -d --build frontend
```

## Backend dependency: operator auth endpoints

The console is wired and typed against the operator-auth surface served by the
backend's public auth router (`gozar/api/auth.py`):

| Method | Path                  | Request                  | Response        |
| ------ | --------------------- | ------------------------ | --------------- |
| POST   | `/api/auth/login`     | `{ username, password }` | `SessionTokens` |
| POST   | `/api/auth/refresh`   | `{ refresh_token }`      | `SessionTokens` |
| GET    | `/api/auth/bootstrap` | -                        | `{ bootstrap_required }` |
| POST   | `/api/auth/bootstrap` | `{ username, password }` | `SessionTokens` |

`SessionTokens` is `{ access_token, refresh_token, token_type, expires_in }`
(matches `gozar/auth/session.py`). The endpoint paths live in one place
(`src/api/config.ts`).
