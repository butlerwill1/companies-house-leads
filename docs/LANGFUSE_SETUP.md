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

The other registries follow the same pattern:

| command | Langfuse entries |
| --- | --- |
| `python -m scripts.screen.search_screen_prompt_registry register` | `search-screen` |
| `python -m scripts.web.web_profile_prompt_registry register` | `web-profile` |
| `python -m scripts.vlm.vlm_prompt_registry register` | the `vlm-financial/` folder: one entry for each of the seven prompts the financial-PDF pipeline sends |

The VLM prompts were registered on 2026-10-03 with their history rebuilt from
git by a one-off script (since removed): nine semantic versions,
`vlm-financials-v1` (2026-07-24) to `v9` (2026-08-18, the current one). Each
Langfuse version's commit message names the git commit it came from. The
seven entries share the version labels, and a sync relabels any entry whose
text has not changed instead of publishing it again (`sync_prompt` in
`langfuse_prompts.py`). So a new Langfuse version of, say,
`vlm-financial/locator` always means the locator text changed, and its
Langfuse number will not match the semantic one (the locator is at Langfuse v4
for `vlm-financials-v9`). Some entries start late because the prompt did not
exist earlier: employee extraction and the two recovery prompts at v4, the
completeness recovery at v6.

## Backups

`~/langfuse-server/backup.ps1` writes all three Langfuse stores to
`~/OneDrive/Backups/companies-house-leads/langfuse/`:

| output | what it holds |
| --- | --- |
| `langfuse-postgres.dump` | metadata -- projects, datasets, dataset items, annotation queues, the prompt registry, users, API keys (`pg_dump --format=custom`) |
| `langfuse-clickhouse.tar.gz` | the trace history: every non-view table exported in Native format and tarred |
| `minio/` | the S3 bucket -- raw ingestion event blobs and uploaded media, including the VLM harness's PDF page images |

Redis is deliberately left out: it holds queue and cache state that Langfuse
rebuilds on boot.

Only the latest copy is kept -- each run overwrites the previous one, with no
dated snapshots and no retention window. Every store is staged to a temporary
path and only moved into place once it is complete and non-empty, so a run
that fails part-way leaves the previous backup intact rather than truncating
the only copy. The MinIO bucket is mirrored object-by-object (not tarred) so
an unchanged blob is not rewritten and OneDrive re-uploads only what moved;
that mirror's `--remove` is guarded by an object count, so a bucket that
reads back empty skips the step instead of emptying the backup.

`BACKUP-INFO.txt` in that folder records when the latest copy was taken, the
image version it came from, the size of each store, and the restore commands
-- nothing is dated, so it is the only record of backup freshness.

Note when reading a restored ClickHouse dump: on Langfuse v4 the data lives in
`events_full` / `events_core` / `scores`, and the legacy `traces` and
`observations` tables are empty. That is expected, not a truncated backup.

Run it with:

```
powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\langfuse-server\backup.ps1
```

It is registered as the Windows Scheduled Task `Langfuse-Backup`, daily at
03:15 (15 minutes after `CompaniesHouseLeads-DBBackup`, which covers the
SQLite stores), with "start when available" so a slot missed while the machine
was off runs at next boot. The stack has to be up when it runs -- the script
dumps through the running containers and fails fast, leaving the previous
backup intact, if `postgres`, `clickhouse` or `minio` is down. If the task
ever needs re-creating:

```
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$env:USERPROFILE\langfuse-server\backup.ps1`""
$t = New-ScheduledTaskTrigger -Daily -At 03:15
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName Langfuse-Backup -Action $a -Trigger $t -Settings $s
```

## Relationship to MLflow

MLflow has been removed. A one-off migration script (since deleted) copied every
MLflow run, trace and human label into Langfuse with their original timestamps:
each run is a session named after it, a run-summary trace carries its params and
metrics, and runs whose cases were still in the gold set are also dataset
experiments. On 2026-10-04 each run's full `report.json` was attached to its
run-summary trace (a "run report" span), a duplicate first migration attempt was
deleted, and superseded labels were pruned to the latest one per field (backups
and scripts in `logs/langfuse-dedupe-2026-10-04/`). The MLflow server was deleted
on 2026-10-05.
