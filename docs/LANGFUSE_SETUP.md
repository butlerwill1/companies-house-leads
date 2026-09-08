# Langfuse setup

Langfuse is the eval-tracing and review backend for both harnesses
(`scripts/profile/business_profile_eval.py`,
`scripts/vlm/vlm_financial_eval.py`). It replaces MLflow. Like the old MLflow
server it runs as a Docker Compose stack **outside this repo**, in
`~/langfuse-server/`, and the repo never carries the compose file.

One Langfuse instance. One **project** per harness (the equivalent of an MLflow
experiment). One API key pair per project. A new harness gets a new project
inside the same instance -- never a second instance, never a different host.

## Stack

`~/langfuse-server/docker-compose.yaml` -- pinned copy of Langfuse's official
self-host compose (`langfuse-web` + `langfuse-worker` + `postgres` +
`clickhouse` + `redis` + `minio`). All ports bound to `127.0.0.1`. Secrets and
the first-boot bootstrap come from `~/langfuse-server/.env` (`.env.example`
beside it is the template; generate secrets with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`, and
`ENCRYPTION_KEY` with `python -c "import secrets; print(secrets.token_hex(32))"`).

| Service | Local URL | Purpose |
|---|---|---|
| langfuse-web | http://localhost:3000 | UI + API |
| minio (S3) | http://localhost:9090 | media blobs (attached PDFs); browser fetches presigned URLs here |
| postgres | localhost:5432 | projects, users, datasets, prompts, annotation queues, score configs |
| clickhouse | localhost:8123 / 9000 | traces, observations, scores |
| redis | localhost:6379 | ingestion queue |

## First run

```bash
docker compose -f ~/langfuse-server/docker-compose.yaml up -d
```

On Windows, `scripts/langfuse_up.ps1` wraps this: it starts Docker Desktop if
its daemon is down, waits for it, runs the compose command, and polls
langfuse-web health. Idempotent -- run it any time you need the stack up.

```
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\langfuse_up.ps1
```

The `LANGFUSE_INIT_*` vars in `~/langfuse-server/.env` bootstrap, on first boot
only:

- org **companies-house-leads**
- the first user (log in at http://localhost:3000 with
  `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`)
- project **business-profile-eval**, with the API key pair from
  `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` -- already copied into the
  repo `.env` as `LANGFUSE_PUBLIC_KEY_BUSINESS_PROFILE` /
  `LANGFUSE_SECRET_KEY_BUSINESS_PROFILE`.

Then create the second project by hand:

1. http://localhost:3000 -> the companies-house-leads org -> **New project** ->
   name it `vlm-financial-eval`.
2. Project **Settings -> API keys -> Create** -> copy the public + secret keys
   into the repo `.env` as `LANGFUSE_PUBLIC_KEY_VLM_FINANCIAL` /
   `LANGFUSE_SECRET_KEY_VLM_FINANCIAL`.

## Repo config

`.env` (repo root, gitignored -- see `.env.example`):

```
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY_BUSINESS_PROFILE=pk-lf-...
LANGFUSE_SECRET_KEY_BUSINESS_PROFILE=sk-lf-...
LANGFUSE_PUBLIC_KEY_VLM_FINANCIAL=pk-lf-...
LANGFUSE_SECRET_KEY_VLM_FINANCIAL=sk-lf-...
```

Each eval config YAML carries a `langfuse:` block that selects which key pair to
use (`key_env: BUSINESS_PROFILE` or `VLM_FINANCIAL`) -- keys themselves never go
in the YAML.

```bash
python -m pip install -r requirements-eval.txt
```

## Backups

`~/langfuse-server/backup.ps1` dumps Postgres (metadata), ClickHouse (trace
history, per-table Native format), and mirrors the MinIO media bucket to
`~/OneDrive/Backups/companies-house-leads/langfuse/`, 30-day retention. Register
it next to the existing DB backup task:

```
schtasks /create /tn "Langfuse-Backup" /tr "powershell -NoProfile -File %USERPROFILE%\langfuse-server\backup.ps1" /sc daily /st 03:15
```

## Relationship to MLflow

MLflow is being removed. During the migration the old server in `~/Documents/mlflow-server-2026-08-27/`
stays up so `scripts/eval_support/migrate_mlflow_to_langfuse.py` can read its
traces; after cutover it is stopped (`docker compose -f ~/Documents/mlflow-server-2026-08-27/compose.yaml down`,
volumes kept) and left parked for ~1 month as a rollback, still covered by
`scripts/backup_databases.py`. Once Langfuse is proven, delete `~/Documents/mlflow-server-2026-08-27/`.
