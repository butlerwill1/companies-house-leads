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

## Prompt management

`python -m scripts.profile.business_profile_prompt_registry register` publishes
`PROMPT_TEMPLATE` to the **Prompts** tab as a new version of
`business-profile-extraction`. Two things about it are easy to misread.

**Langfuse's version number is not the prompt version.** Langfuse assigns an
auto-incrementing number per prompt name that counts *registrations* and
cannot be set. Eight registrations have produced six semantic versions, so
entry **#8 is `business-profile-v6`**. The numbers will never line up and no
attempt is made to align them. The semantic version is carried as a **label**,
which is per-version and is what
[`registered_prompt_reference`](../scripts/eval_support/langfuse_prompts.py)
reads to produce a run's traceability string:

```
business-profile-extraction@business-profile-v6 [langfuse v8]
```

**The version name is also written as a tag -- ignore it.** Langfuse tags are
per-prompt, not per-version: every registration rewrites the tag set across
all versions of that name. After v6 was published, all eight entries report
`tags=['business-profile-v6']`, including two published on 2 September before
v6 existed. The tag is kept because it is what the UI filters on and it is
accurate for the current version; nothing may use it to work out which
semantic version an entry came from. Until 2026-09-10 the reference function
did exactly that, which meant rolling the `production` label back to an older
entry would still report the newest version's name.

**What the entry contains.** The six `{{<field>_options}}` blocks -- the value
lists and glosses built from `FIELD_DEFINITIONS` -- are baked into the
registered text. The per-case variables (`{{company_name}}`,
`{{sections_block}}`, `{{sic_label}}`, `{{sic_code}}`) stay as placeholders,
so the entry still reads as a template. This changed on 2026-09-10: before it,
only the skeleton was stored, and since nearly every prompt version changes a
gloss rather than the skeleton, the registry recorded almost nothing.
Registrations **3 through 7 are byte-identical** across semantic v4 and v5,
even though v5 added three `delivery_model` values and rewrote three glosses.
Entries from v6 onward are diffable; for anything earlier, the code history is
the only record.

Nothing builds a prompt *from* Langfuse -- `build_prompt` formats the Python
template directly, and `get_prompt` is used only for the traceability string
-- so the registry is a record, not a dependency. An eval run never fails
because a prompt was not registered; the reference just comes back `None`.

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
