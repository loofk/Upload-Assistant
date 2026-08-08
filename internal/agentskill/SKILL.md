---
name: upload-assistant
description: Operate the local Upload Assistant v2 HTTP service for Chinese PT retorrent jobs, daily candidate recommendations and schedules, rule review, duplicate checks, integrations, evidence, notifications, and task recovery. Use when an agent must create, inspect, pause, resume, schedule, or audit a durable PT workflow without bypassing accept_rules, confirm_upload, site obligations, duplicate gates, or seeding requirements.
---

# Upload Assistant

Use the service as an auditable workflow controller. Treat every rule, duplicate, upload, and seeding gate as mandatory.

## Connect

1. Use the operator-provided base URL. The default deployment binds only to the local host.
2. Check `GET /health/ready` without authentication.
3. Load `GET /openapi.json` for the canonical request and response schemas.
4. Load authenticated `GET /api/v2/tools` for the current tool catalog.
5. Send the operator-provided token as `Authorization: Bearer <token>`.

Prefer native HTTP or OpenAPI tools. If a shell is the only transport, use environment variables such as `UA_BASE_URL` and `UA_API_TOKEN`. Never put tokens in command arguments, URLs, prompts, logs, reports, or repository files. Never search the filesystem for credentials.

## Choose an Operation

- List jobs or read one job before deciding whether to create work.
- Create a retorrent job only from an explicit source URL and target site request. Use a fresh idempotency key for a new intent.
- Create or read daily candidate jobs when the operator wants recommendations. Treat schedules and in-app notifications as discovery only.
- Read status and summary for normal progress. Read events and verified artifacts for an audit.
- Resume only with the values named by `blockers`, `next_actions`, and `resume_state`.
- Change rules, downloaders, image hosts, screenshot profiles, or site credentials only when the operator explicitly asks.

## Run a Retorrent Workflow

1. Create the job without setting `accept_rules` or `confirm_upload` unless the user explicitly supplied that exact consent for this job.
2. Poll with bounded intervals and report durable step transitions. Do not hide or reinterpret blockers.
3. Require the exact active rule fingerprint and every unresolved manual obligation before accepting rules.
4. Require explicit live-upload confirmation only after the user can review the immutable upload package, duplicate result, active rules, and remaining obligations.
5. After upload, continue through target torrent download, qBittorrent injection, seeding verification, and final summary.
6. Call the job complete only when the API reports `status=complete`, `ok=true`, no blockers, and a persisted `summary_file`.

When an operator wants stepwise control, use `execution_mode=step` or `stop_after_step`. A paused job is an expected control boundary, not a failure.

## Run Daily Candidate Discovery

1. Create a one-off `daily_candidates` job or configure a daily schedule with the source, target, daily cron expression, and IANA timezone requested by the operator.
2. Read the schedule's run history before diagnosing a missed trigger or retry; use its durable status, job link, attempt count, and safe error field.
3. Read the candidate job summary and persisted candidate list. Require rule snapshot, source downloadability, required listing fields, metadata, and a clear target duplicate check before recommending an item.
4. Read in-app notifications as local delivery evidence only. A notification never means the user approved a candidate.
5. Create a retorrent job from a selected candidate only when the operator requests it. The created job starts with no inferred rule acceptance and `confirm_upload=false`.
6. Keep candidate submission, rule acceptance, and final live-upload confirmation as separate decisions.

## Enforce Safety Gates

- Never bypass rule acceptance, manual obligations, duplicate checks, upload confirmation, or seeding requirements.
- Never reuse a source tracker torrent as the target tracker torrent.
- Never retry an upload whose remote outcome is unknown. Stop for reconciliation to avoid duplicate publication.
- Treat `blocked` and `failed` as non-success states. Preserve their evidence and recovery instructions.
- Stop when the target duplicate gate reports an existing or disallowed resource.
- Do not reveal cookies, passkeys, announce URLs, API keys, signed URLs, raw torrent bytes, or decrypted credentials.
- Cancellation does not delete downloaded data or audit artifacts. Do not claim otherwise.
- Do not run live tracker, downloader, or image-host probes merely to test connectivity without operator authorization.
- Never interpret a schedule firing, candidate rank, or notification as permission to submit a candidate or upload a torrent.

## Manage Rules and Configuration

Rule changes follow an immutable review sequence: import Markdown as a draft, inspect the parsed policy and original text, have a human approve the exact fingerprint, then activate the approved revision. Missing, incomplete, unapproved, or stale rules must block automation.

Credentials are write-only inputs. Confirm successful storage from redacted API responses; never try to read secrets back. Keep downloader path mappings explicit and validate them before running jobs.

## Interpret Responses

Use `status`, `ok`, `blockers`, `next_actions`, `job_id`, `summary`, `summary_file`, and `resume_state` as the decision path. Keep operator-facing reports concise: current step, verified evidence, blockers, required decision, and next safe action.
