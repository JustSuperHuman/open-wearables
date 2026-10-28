# Deploying Open Wearables to Fly.io

Fly.io does not run `docker-compose.yml` directly. This setup maps the compose
stack to Fly apps:

| Compose service | Fly resource                          | Notes                                   |
| --------------- | ------------------------------------- | --------------------------------------- |
| `db`            | Fly Managed Postgres                  | Managed cluster                         |
| `redis`         | `open-wearables-redis`                | Internal Redis app, private 6PN only    |
| `app`           | `open-wearables-api` process `web`    | Public FastAPI app on `:8000`           |
| `celery-worker` | `open-wearables-api` process `worker` | Same backend image                      |
| `celery-beat`   | `open-wearables-api` process `beat`   | Same backend image                      |
| `flower`        | `open-wearables-flower`               | Public Celery dashboard with basic auth |
| `svix-server`   | `open-wearables-svix`                 | Internal webhook server on `.flycast`   |
| `frontend`      | `open-wearables-frontend`             | SvelteKit (Bun) server on `:3000`       |

Config files in this repo:

- `backend/fly.toml` - API, worker, and beat process groups
- `frontend/fly.toml` - frontend SvelteKit server
- `deploy/fly/redis.toml` - internal Redis
- `deploy/fly/flower.toml` - Flower
- `deploy/fly/svix.toml` - Svix
- `deploy.ts` - optional Bun helper for deploying components in order
- `.env.fly.*.example` - safe templates for ignored local deployment env files

## 0. Prerequisites and Env Files

```bash
fly auth login
bun --version
```

The examples below use the default app prefix `open-wearables` and region `iad`.
Override them without editing tracked files:

```bash
export FLY_APP_PREFIX=my-wearables
export FLY_REGION=iad
export FLY_ORG=my-fly-org
export FLY_API_URL=https://api.example.com
export FLY_FRONTEND_URL=https://app.example.com
```

`deploy.ts` automatically loads `.env.fly.local` when present. That file is
ignored by git, so app names, organization, domains, and other
deployment-specific public values can stay local:

```bash
cp .env.fly.local.example .env.fly.local
```

Example `.env.fly.local`:

```env
FLY_ORG=my-fly-org
FLY_REGION=iad
FLY_API_URL=https://api.example.com
FLY_FRONTEND_URL=https://app.example.com
FLY_CORS_ORIGINS=https://app.example.com
```

Set `FLY_ENV_FILE=/path/to/env-file` to load a different file. Shell environment
variables always take precedence over file values.

You can also override individual app names:

```bash
export FLY_API_APP=my-wearables-api
export FLY_FRONTEND_APP=my-wearables-frontend
export FLY_FLOWER_APP=my-wearables-flower
export FLY_REDIS_APP=my-wearables-redis
export FLY_SVIX_APP=my-wearables-svix
```

Local files such as `.env.fly.local` are ignored by git, so
deployment-specific values can live outside the open-source branch.

Use separate ignored files for secrets:

| File                      | Fly app  | Contents                                                                   |
| ------------------------- | -------- | -------------------------------------------------------------------------- |
| `.env.fly.backend.local`  | API      | Database, `SECRET_KEY`, admin credentials, provider credentials, Sentry/S3 |
| `.env.fly.flower.local`   | Flower   | Database, `SECRET_KEY`, `FLOWER_BASIC_AUTH`                                |
| `.env.fly.svix.local`     | Svix     | `SVIX_JWT_SECRET`, `SVIX_DB_DSN`, `SVIX_REDIS_DSN`                         |
| `.env.fly.frontend.local` | Frontend | Optional public `VITE_*` values only                                       |

Copy the examples, fill the local files, then sync secrets without printing their
values:

```bash
cp .env.fly.backend.local.example .env.fly.backend.local
cp .env.fly.flower.local.example .env.fly.flower.local
cp .env.fly.svix.local.example .env.fly.svix.local

bun run deploy secrets --dry-run
bun run deploy secrets
```

Blank values are skipped, so these files can be used for partial rotations. For
example, a backend file containing only `STRAVA_CLIENT_SECRET=...` will update
only that Fly secret.

## 1. Managed Postgres

```bash
fly mpg create --name open-wearables-db --region iad
```

Save the generated host, port, database, user, and password. The backend expects
individual database variables:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

The database user must be able to create the `svix` database. The backend startup
script runs `scripts/init/create_svix_db.py` before starting the API.

## 2. Internal Redis

Deploy the private Redis app:

```bash
bun run deploy redis
```

By default the backend reaches Redis at:

```text
open-wearables-redis.internal:6379
```

If you use a managed Redis instead, skip `bun run deploy redis` and set
`REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`, and `REDIS_SSL`
on the API and Flower apps with `fly secrets set`.

## 3. Backend Secrets

Set the required backend secrets in `.env.fly.backend.local`, then sync them:

```bash
cp .env.fly.backend.local.example .env.fly.backend.local
bun run deploy secrets
```

Also set any provider credentials you need from `backend/config/.env.example`,
such as `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, Sentry, and S3/FIT settings.

Do not commit `.env` files. `backend/config/.env` is intentionally excluded from
Docker builds; Fly should receive secrets through `fly secrets set`.

## 4. Svix

Svix needs its own app and secrets. Use the same `SVIX_JWT_SECRET` value as the
backend.

```bash
cp .env.fly.svix.local.example .env.fly.svix.local
bun run deploy secrets
bun run deploy svix
```

The backend reaches Svix at:

```text
http://open-wearables-svix.flycast:8071
```

The deploy helper passes `SVIX_SERVER_URL` to the API app automatically. If you
deploy manually, include it:

```bash
cd backend
fly deploy --config fly.toml \
  --app open-wearables-api \
  --env SVIX_SERVER_URL=http://open-wearables-svix.flycast:8071
cd ..
```

## 5. Backend App

Deploy the API, worker, and beat process groups:

```bash
bun run deploy api
```

The helper sets non-secret runtime values during deploy:

- `API_BASE_URL`
- `REDIS_HOST`
- `SVIX_SERVER_URL`
- `CORS_ORIGINS`

For a custom domain, set `FLY_API_URL` before deploying:

```bash
export FLY_API_URL=https://api.example.com
bun run deploy api
```

## 6. Flower

Flower reuses the latest backend image. It needs the same database and auth
secrets as the backend because it imports the app settings:

```bash
cp .env.fly.flower.local.example .env.fly.flower.local
bun run deploy secrets
bun run deploy flower
```

If `FLOWER_BASIC_AUTH` is missing, `deploy.ts` generates one as `admin:<password>`
and prints it once. Set your own value if you need deterministic credentials:

```bash
fly secrets set -a open-wearables-flower \
  FLOWER_BASIC_AUTH="admin:$(openssl rand -hex 16)"
```

## 7. Frontend

Deploy the frontend:

```bash
bun run deploy frontend
```

The helper passes `VITE_API_URL` from `FLY_API_URL` during deploy. The frontend
also reads `VITE_API_URL` at runtime in the SvelteKit server, so you do not need to
rebuild the image to point it at a different API. For a custom API:

```bash
export FLY_API_URL=https://api.example.com
bun run deploy frontend
```

For additional public frontend configuration, copy `.env.fly.frontend.local.example`
to `.env.fly.frontend.local`. Only `VITE_*` keys are passed through; do not put
secrets in frontend env files.

If you use a custom frontend domain, add it to CORS:

```bash
export FLY_FRONTEND_URL=https://app.example.com
bun run deploy api
```

## 8. Deploy Everything

After Postgres and required secrets are ready:

```bash
bun run deploy all
```

The helper deploys components in dependency order:

1. Secrets from ignored local env files, if present
2. Redis
3. API
4. Flower
5. Frontend
6. Svix

If you prefer Svix before API, deploy it separately after setting its secrets:

```bash
bun run deploy secrets redis svix api flower frontend
```

## Notes

- Keep Postgres, Redis, API, Flower, Svix, and frontend in the same region.
- `auto_stop_machines` is disabled because worker, beat, webhooks, and Flower
  should not depend on inbound HTTP traffic to wake up.
- `scripts/start/app.sh` runs migrations and seed tasks on every web boot. This
  is acceptable for one web machine. If you scale the `web` process above one
  machine, move migrations into a Fly release command to avoid concurrent
  migration attempts.
- The default Redis app is intentionally private. Do not add public services or
  public IPs to `deploy/fly/redis.toml`.
- `fly deploy` leaves stopped machines stopped. After a deploy, run
  `fly status -a open-wearables-api` and `fly machine start <id>` any `worker` or
  `beat` machine that isn't `started`. Without the worker, provider webhooks
  (Garmin pushes, Strava events) only queue up in Redis.
- Keep the `beat` machine running: besides the periodic syncs it runs
  `refresh-garmin-tokens-hourly`, which keeps Garmin refresh tokens (90-day life)
  from expiring for users who only ever push data. Without it those connections
  turn `expired` after three months.
- Garmin push URL token. Garmin's API Tools drops query strings when it saves an
  endpoint URL, so the token goes in the path:
  1. `fly secrets set GARMIN_WEBHOOK_TOKEN=$(openssl rand -hex 32) -a open-wearables-api`
     (leave `GARMIN_WEBHOOK_TOKEN_REQUIRED` unset) and deploy.
  2. Change every enabled endpoint in Garmin's API Tools to
     `https://wearables.api.justgains.com/api/v1/providers/garmin/webhooks/t/<token>`,
     then reopen the page and check the saved URL still ends in `/t/<token>`.
  3. Wait until the API logs stop showing `webhook_token_not_enforced`.
  4. `fly secrets set GARMIN_WEBHOOK_TOKEN_REQUIRED=true -a open-wearables-api`.

  Logs and Sentry show the path token as `REDACTED` / `[Filtered]`. Details in
  `docs/providers/garmin-api-integration.mdx` ("Securing the push URL").
- If the web machine hits its restart limit during a deploy, roll back with
  `fly deploy --image registry.fly.io/open-wearables-api:<previous tag>` (tags from
  `fly releases --image -a open-wearables-api`), then `fly machine start` the
  replacement web machine, which Fly creates stopped.

## Production database history

- **2026-10-27, upstream 0.10 merge.** Production was stamped at `a1f4e7c2b9d0`, but
  upstream's `d8a0bc9afdd9` (`menstrual_cycle_details`), an ancestor of that
  revision, had never actually run. Upstream's `b2c3d4e5f6a1` then failed on the
  missing table and the first deploy crash-looped. A schema diff of a prod backup
  against a fresh DB migrated to `a1f4e7c2b9d0` showed that table as the only
  drift. The fix was creating it with the exact SQL from
  `alembic upgrade 264b79d7c541:d8a0bc9afdd9 --sql`, after which `upgrade head`
  ran cleanly. Before deploying a merge that adds migrations, restore a prod dump
  into a local Postgres and run `alembic upgrade head` plus the `app.sh` scripts
  against it first.
