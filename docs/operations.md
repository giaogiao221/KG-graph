# Operations Guide — Extraction Review Platform

Intranet deployment for roughly 10 concurrent users and batches of 100+
documents. All services run under Docker Compose; secrets live only in the
environment, never in images, source, or logs.

## 1. Architecture

| Service      | Image                     | Role                                             |
| ------------ | ------------------------- | ------------------------------------------------ |
| `frontend`   | `frontend/Dockerfile`     | nginx serving the SPA, proxying `/api` and SSE   |
| `api`        | `backend/Dockerfile`      | FastAPI application (uvicorn)                    |
| `worker-rule`| `backend/Dockerfile`      | Celery worker for the `rule` queue + embedded beat scheduler |
| `worker-llm` | `backend/Dockerfile`      | Celery worker for the `llm` queue                |
| `postgres`   | `postgres:16-alpine`      | System of record                                 |
| `redis`      | `redis:7-alpine`          | Celery broker/result backend (AOF persistence)   |
| `minio`      | `minio/minio` (profile)   | Optional, reserved for a future S3 storage driver|
| `mock-llm`   | `python:3.12-slim` (test) | Acceptance-only deterministic model stub         |

Networks: `backend` (postgres, redis, api, workers) and `web` (frontend ↔
api). Only the frontend publishes a host port (default `8080`). PostgreSQL,
Redis, and the API are unreachable from the host except through nginx.

State: named volumes `postgres-data`, `redis-data`, `platform-data`
(uploaded documents, job work directories, exports), `minio-data`.

## 2. Prerequisites

- Docker Engine 24+ with Compose v2 (`docker compose version`).
- No application code checkout is required on the target host beyond this
  repository (the compose file builds both images from source).

## 3. Configuration

```bash
cp .env.example .env
```

Fill in every required value. Generate independent secrets, e.g.:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"                # POSTGRES_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"                # EXTRACTION_JWT_SECRET, EXTRACTION_CACHE_HMAC_KEY
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"  # EXTRACTION_MODEL_MASTER_KEY
```

Every compose command below takes the env file explicitly:

```bash
export COMPOSE="docker compose --env-file .env -f deploy/docker-compose.yml"
```

(PowerShell: `$env:COMPOSE` is not a command; spell the command out or use
`--env-file .env` each time.)

Validate the configuration before building:

```bash
$COMPOSE config
```

## 4. First deployment

```bash
$COMPOSE up -d --build
$COMPOSE ps            # every service healthy
```

Apply database migrations (idempotent; safe to re-run):

```bash
$COMPOSE exec api alembic upgrade head
```

Create the initial administrator (one command, idempotent):

```bash
$COMPOSE exec api python -m app.auth.bootstrap
```

The command reads `EXTRACTION_BOOTSTRAP_ADMIN_USERNAME` /
`EXTRACTION_BOOTSTRAP_ADMIN_PASSWORD` from the environment, or prompts
interactively (preferred on shared hosts). Re-running with an existing
username fails cleanly; to rotate the admin password deliberately:

```bash
$COMPOSE exec api python -m app.auth.bootstrap --reset-password
```

After the account exists, clear the bootstrap password from `.env`.

## 5. Post-install setup (admin UI)

Sign in at `http://<host>:8080` as the bootstrap admin, then:

1. **Admin → accounts**: create local users with roles (`operator` runs
   extraction, `reviewer` reviews, `viewer` reads, `admin` manages).
2. **Projects**: create a project and add members (owner/member).
3. **Admin → models**: register OpenAI-compatible providers. The endpoint
   host must be listed in `allowed_hosts`; enable `allow_private_network`
   only for intranet endpoints. API keys are stored AES-GCM-encrypted and
   are never returned by the API.
4. Optionally add price versions so usage reports carry costs.

## 6. Routine operations

- Stop / start: `$COMPOSE down` / `$COMPOSE up -d` (volumes persist).
- Follow logs: `$COMPOSE logs -f api`, `... worker-rule`, `... worker-llm`.
- Health: `GET /api/health` through nginx; container health via `$COMPOSE ps`.
- Audit trail: security-relevant events (exports, bootstrap, review actions)
  are append-only rows in the `audit_events` table — query with
  `$COMPOSE exec postgres psql -U extraction -d extraction`.

## 7. Backup

Quiesce writers for a consistent snapshot (or accept a crash-consistent one):

```bash
$COMPOSE stop worker-rule worker-llm api
$COMPOSE exec postgres pg_dump -U extraction -d extraction -Fc > backup-$(date +%Y%m%d).pgdump
docker run --rm -v extraction-platform_platform-data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/storage-$(date +%Y%m%d).tgz -C /data .
$COMPOSE start api worker-rule worker-llm
```

Redis holds only queue state; its AOF is inside `redis-data` and normally
does not need separate backup.

## 8. Restore

```bash
$COMPOSE up -d postgres
cat backup-YYYYMMDD.pgdump | $COMPOSE exec -T postgres pg_restore -U extraction -d extraction --clean
docker run --rm -v extraction-platform_platform-data:/data -v "$PWD:/backup" alpine \
  tar xzf /backup/storage-YYYYMMDD.tgz -C /data
$COMPOSE up -d
```

## 9. Scaling workers

- `worker-llm` scales freely: `$COMPOSE up -d --scale worker-llm=3`
  (remove the fixed `--hostname` or give each replica its own when scaling:
  override the command in a local `docker-compose.override.yml`).
- `worker-rule` must stay at **exactly one replica**: it embeds the Celery
  beat scheduler for the periodic dispatch-outbox, lease-recovery, and
  cleanup tasks. The scheduled tasks are fenced and idempotent, but running
  multiple beats wastes work. To scale rule throughput, split beat into its
  own service first.

## 10. Key rotation

- **`EXTRACTION_JWT_SECRET`** — replace in `.env`, `$COMPOSE up -d`. All
  existing login sessions are invalidated; users sign in again.
- **`EXTRACTION_MODEL_MASTER_KEY`** — encrypts stored model API keys. Set the
  new key, `$COMPOSE up -d`, then re-enter every provider API key through
  Admin → models (secret rotation endpoint). Old ciphertext is unreadable
  once the old key is gone; do this during a maintenance window.
- **`EXTRACTION_CACHE_HMAC_KEY`** — protects the worker-side model result
  cache. Rotating invalidates cached model units (harmless; they recompute).
- **`POSTGRES_PASSWORD`** — change via `ALTER USER extraction PASSWORD`, then
  update `.env` and `$COMPOSE up -d`.

## 11. Acceptance verification

Backend unit/integration tests (inside the api image):

```bash
$COMPOSE exec api python -m pytest
```

PostgreSQL-only constraint tests run when a DSN is provided (they create and
migrate the named database, then roll back their writes):

```bash
$COMPOSE exec api sh -c \
  'EXTRACTION_TEST_POSTGRES_DSN=postgresql+psycopg://extraction:$POSTGRES_PASSWORD@postgres:5432/extraction_it python -m pytest tests/integration'
```

Frontend unit tests and production build (on a dev machine with Node 22):

```bash
cd frontend
npm ci
npm test -- --run
npm run build
```

End-to-end acceptance (Playwright) against a throwaway stack with the
deterministic model stub — no paid external model is called:

```bash
$COMPOSE --profile test up -d --build
$COMPOSE exec api alembic upgrade head
$COMPOSE exec api python -m app.auth.bootstrap   # set the admin password
cd frontend
npx playwright install chromium                   # first time only
E2E_ADMIN_PASSWORD=<the bootstrap password> npx playwright test
```

The E2E seeds its own users/project/document/model config idempotently and
completes its review task, so it can run repeatedly against the same stack.
Tear the acceptance stack down with `$COMPOSE --profile test down -v` to
reset everything.

## 12. Security notes

- Secrets arrive only through the environment; `.env` is git-ignored and the
  Docker build context excludes it (`.dockerignore`).
- Passwords, API keys, and bearer tokens never appear in API responses,
  audit metadata, or logs; the API masks stored model keys as `********`.
- `D:\KGchouqu_clean` / `D:\KGchouqu_data` legacy roots are not mounted into
  any container; the platform works from uploaded copies only.
- Upload size is capped (`client_max_body_size 30m` in nginx, enforced again
  by the API); SSE progress is proxied unbuffered by a dedicated nginx
  location.
