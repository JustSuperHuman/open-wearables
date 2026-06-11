# Deploying Open Wearables to Fly.io

Fly.io does **not** run `docker-compose.yml`. Each service is deployed as its own
Fly app (or as a process group within an app) using a `fly.toml`. This guide maps
the compose stack onto Fly and gives the exact command sequence.

## Architecture on Fly

| Compose service | Fly resource | Notes |
|---|---|---|
| `db` (postgres:18) | **Fly Managed Postgres** | Managed cluster, not self-hosted |
| `redis` (redis:8) | **Upstash Redis** (`fly redis create`) | Managed, private-network URL |
| `app` | `open-wearables-api` — process `web` | Public, :8000 |
| `celery-worker` | `open-wearables-api` — process `worker` | Same image, no ports |
| `celery-beat` | `open-wearables-api` — process `beat` | Same image, no ports |
| `flower` | `open-wearables-flower` | Separate app, same image, public + basic auth |
| `svix-server` | `open-wearables-svix` | Separate app, internal-only (`.flycast`) |
| `frontend` | `open-wearables-frontend` | Nitro node server, :3000 |

Config files in this repo:
- `backend/fly.toml` — API + worker + beat (process groups)
- `deploy/fly/flower.toml` — Flower
- `deploy/fly/svix.toml` — Svix
- `frontend/fly.toml` — frontend

> Set `primary_region` in each toml to your closest region (`fly platform regions`
> to list). They default to `syd`.

---

## 0. Prerequisites

```bash
# Install flyctl, then:
fly auth login
```

---

## 1. Managed Postgres

```bash
fly mpg create --name open-wearables-db --region syd
```

Note the connection details. The backend builds its DSN from individual vars
(`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`), so capture each.
The connecting user needs `CREATEDB` — `scripts/start/app.sh` creates a separate
`svix` database on the same cluster via `create_svix_db.py`.

---

## 2. Upstash Redis

```bash
fly redis create --name open-wearables-redis --region syd
fly redis status open-wearables-redis   # shows the private redis:// URL + password
```

The URL looks like `redis://default:<password>@fly-open-wearables-redis.upstash.io:6379`.
Capture the host and password — the app builds the URL from `REDIS_HOST`,
`REDIS_PORT`, `REDIS_USERNAME` (`default`), `REDIS_PASSWORD`.

> The app's `redis_url` builder emits a plain `redis://` URL (no TLS). Fly's
> Upstash Redis is reachable over the private network without TLS, so this works.

---

## 3. Backend app (web + worker + beat)

```bash
cd backend
fly launch --no-deploy --copy-config --name open-wearables-api
```

Set secrets (single command):

```bash
fly secrets set -a open-wearables-api \
  DB_HOST=<mpg-host> DB_USER=<mpg-user> DB_PASSWORD=<mpg-password> \
  REDIS_HOST=fly-open-wearables-redis.upstash.io REDIS_USERNAME=default REDIS_PASSWORD=<redis-password> \
  SECRET_KEY="$(openssl rand -hex 32)" \
  ADMIN_EMAIL=admin@yourdomain.com ADMIN_PASSWORD="$(openssl rand -hex 16)" \
  SVIX_JWT_SECRET="$(openssl rand -hex 32)"
```

Also copy any provider/API credentials from `backend/config/.env.example`
(OAuth client IDs/secrets, Sentry DSN, S3/FIT settings, etc.) into `fly secrets set`.
**Do not** commit `.env` or pass it as an env_file.

Deploy:

```bash
fly deploy        # builds backend/Dockerfile, starts web + worker + beat
```

The API is now at `https://open-wearables-api.fly.dev`.

---

## 4. Svix (webhook server, internal-only)

```bash
fly apps create open-wearables-svix
fly secrets set -a open-wearables-svix \
  SVIX_JWT_SECRET=<same-as-backend> \
  SVIX_DB_DSN="postgresql://<mpg-user>:<mpg-password>@<mpg-host>:5432/svix" \
  SVIX_REDIS_DSN="redis://default:<redis-password>@fly-open-wearables-redis.upstash.io:6379/1"
fly deploy --config deploy/fly/svix.toml --image svix/svix-server:v1
```

The backend reaches it at `http://open-wearables-svix.flycast:8071`. Point the
backend's Svix server URL secret at that (check `.env.example` for the exact var
name, e.g. `SVIX_SERVER_URL`):

```bash
fly secrets set -a open-wearables-api SVIX_SERVER_URL="http://open-wearables-svix.flycast:8071"
```

---

## 5. Flower (public, basic auth)

```bash
IMG=$(fly image show -a open-wearables-api --json | jq -r '.Ref')
fly apps create open-wearables-flower
fly secrets set -a open-wearables-flower \
  DB_HOST=<mpg-host> DB_USER=<mpg-user> DB_PASSWORD=<mpg-password> \
  REDIS_HOST=fly-open-wearables-redis.upstash.io REDIS_USERNAME=default REDIS_PASSWORD=<redis-password> \
  SECRET_KEY=<same-as-backend> \
  FLOWER_BASIC_AUTH="admin:$(openssl rand -hex 16)"
fly deploy --config deploy/fly/flower.toml --image "$IMG"
```

Flower is at `https://open-wearables-flower.fly.dev` behind the basic-auth
credentials you set. (`flower.sh` waits for a worker `ping` over the Redis broker,
so the backend app must be up first.)

---

## 6. Frontend

`VITE_API_URL` is baked in at build time, so pass it as a build arg:

```bash
cd frontend
fly launch --no-deploy --copy-config --name open-wearables-frontend
fly deploy --build-arg VITE_API_URL=https://open-wearables-api.fly.dev
```

Frontend is at `https://open-wearables-frontend.fly.dev`. Finally, add it to the
backend CORS allow-list:

```bash
fly secrets set -a open-wearables-api CORS_ORIGINS='["https://open-wearables-frontend.fly.dev"]'
```

---

## Notes & gotchas

- **Migrations** run on every web boot via `app.sh`. Fine for one web machine;
  if you scale web > 1, split migrations into a `release_command` (see comment in
  `backend/fly.toml`).
- **Volumes**: nothing persistent lives in the app containers — Postgres/Redis
  are managed, so no Fly volumes are needed unless you start writing FIT files to
  local disk (you're using S3 per PR #1099, so keep it remote).
- **Region**: keep Postgres, Redis, and all apps in the **same region** to avoid
  cross-region latency on every DB/broker call.
- **Costs**: with `auto_stop_machines`, idle worker/beat still need to stay up to
  process the queue — they have no HTTP traffic to wake them, so keep
  `min_machines_running >= 1` for those (the backend `[[vm]]` covers web/worker/beat
  on one machine each).
