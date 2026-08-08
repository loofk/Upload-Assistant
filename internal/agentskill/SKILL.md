---
name: upload-assistant
description: Operate the local Upload Assistant v2 HTTP service for Chinese PT retorrent jobs, daily candidate recommendations and schedules, rule review, duplicate checks, integrations, safe legacy configuration migration, evidence, notifications, and task recovery. Use when an agent must create, inspect, pause, resume, schedule, migrate, or audit a durable PT workflow without bypassing fingerprints, explicit confirmations, accept_rules, confirm_upload, site obligations, duplicate gates, or seeding requirements.
---

# Upload Assistant

Use the service as an auditable workflow controller. Treat every rule, duplicate, upload, and seeding gate as mandatory.

## Connect

1. Use the operator-provided base URL. The default deployment binds only to the local host.
2. Check `GET /health/ready` without authentication.
3. Load `GET /openapi.json` for the canonical request and response schemas.
4. Load authenticated `GET /api/v2/tools` for the current tool catalog.
5. Send the operator-provided token as `Authorization: Bearer <token>`.

Prefer native HTTP or OpenAPI tools. If a shell is the only transport, use `upload-assistant cli` with `UA_API_URL` and `UA_API_TOKEN_FILE`; `UA_API_TOKEN` is also supported for a controlled process environment. Never put tokens in command arguments, URLs, logs, reports, or repository files. Never search the filesystem for credentials. The CLI blocks non-loopback plaintext HTTP unless the operator explicitly allows it.

The native CLI returns the same structured JSON as the API. Use `jobs summary`, `jobs steps`, `jobs attempts`, `jobs events`, and `jobs artifacts` for job audit reads; use `audit list` for redacted global configuration and external-action records. Read attempts before retrying, replaying, or diagnosing a worker interruption: each record carries the durable attempt number, timing, stable error code, redacted result, and input-snapshot SHA-256. Use `jobs pause`, `jobs resume`, `jobs retry`, `jobs replay`, and `jobs cancel` only for the matching operator intent. The interactive `shell` uses the same command parser and safety checks. For live consent, pass exact `--accept-rule SITE=FINGERPRINT` and `--obligation SITE:ID=EVIDENCE` values; `--confirm-upload` never infers them.

Before any external probe or controlled live workflow, call `GET /api/v2/readiness/live` (or CLI `readiness live`) with the requested U2/CHD source, MTEAM target, downloader, image host, screenshot profile, TMDb provider, and PTGen provider. It checks local configuration only, including the native MediaInfo, BDInfo, FFmpeg, FFprobe, and mkbrr binaries. It never contacts a tracker, downloader, image host, metadata provider, or media manager; it always returns `external_calls_performed=false`, `live_upload_authorized=false`, and `resume_state.confirm_upload=false`. Show the operator its blockers, next actions, exact rule fingerprints, and obligation IDs. Never treat `configuration_ready=true` as credential validation, duplicate clearance, network-probe consent, rule acceptance, or upload consent.

## Choose an Operation

- List jobs or read one job before deciding whether to create work.
- Create a retorrent job only from an explicit source URL and target site request. Use a fresh idempotency key for a new intent.
- Create or read daily candidate jobs when the operator wants recommendations. Treat schedules and every notification channel as discovery only.
- Read status and summary for normal progress. Read step attempts before deciding whether a failed/blocked step is safe to resume or retry; correlate their IDs with job events and verified artifacts for a workflow integrity audit. Read `/api/v2/audit-events` for global configuration or external-action history; it is redacted and paginated but is not the per-job hash chain.
- Resume only with the values named by `blockers`, `next_actions`, and `resume_state`.
- Replay only when the operator explicitly wants a fresh job after inspecting attempts and events. Replay is limited to blocked, failed, or cancelled jobs; it must reject running, paused, complete, reconciliation-required, and unknown-outcome jobs. The new job is linked by `replay_of_job_id`, defaults to step mode, and never inherits `accept_rules`, obligation evidence, resume state, or `confirm_upload`.
- Change rules, downloaders, image hosts, notification channels, Sonarr/Radarr instances, TMDb/PTGen metadata providers, screenshot profiles, or site credentials only when the operator explicitly asks.
- Preview or execute legacy configuration migration only when the operator explicitly asks.

## Run a Retorrent Workflow

1. Run the local-only live readiness check and resolve local blockers. Obtain separate operator authorization before any external probe.
2. Create the job without setting `accept_rules` or `confirm_upload` unless the user explicitly supplied that exact consent for this job.
3. Poll with bounded intervals and report durable step transitions. Do not hide or reinterpret blockers.
4. Require the exact active rule fingerprint and every unresolved manual obligation before accepting rules.
5. Require explicit live-upload confirmation only after the user can review the immutable upload package, duplicate result, active rules, and remaining obligations.
6. After upload, continue through target torrent download, configured target-downloader injection, seeding verification, and final summary.
7. Call the job complete only when the API reports `status=complete`, `ok=true`, no blockers, and a persisted `summary_file`.

When an operator wants stepwise control, use `execution_mode=step` or `stop_after_step`. A paused job is an expected control boundary, not a failure.

## Run Daily Candidate Discovery

1. Create a one-off `daily_candidates` job or configure a daily schedule with the source, target, daily cron expression, and IANA timezone requested by the operator.
2. Read the schedule's run history before diagnosing a missed trigger or retry; use its durable status, job link, attempt count, and safe error field.
3. Read the candidate job summary and persisted candidate list. Require rule snapshot, source downloadability, required listing fields, metadata, and a clear target duplicate check before recommending an item.
4. Read notification records as delivery evidence only. External delivery requires an existing enabled channel explicitly named in that schedule; `sent` requires a persisted remote receipt. A notification never means the user approved a candidate.
5. Create a retorrent job from a selected candidate only when the operator requests it. The created job starts with no inferred rule acceptance and `confirm_upload=false`.
6. Keep candidate submission, rule acceptance, and final live-upload confirmation as separate decisions.

## Enforce Safety Gates

- Never bypass rule acceptance, manual obligations, duplicate checks, upload confirmation, or seeding requirements.
- Never reuse a source tracker torrent as the target tracker torrent.
- Never retry an upload whose remote outcome is unknown. Stop for reconciliation to avoid duplicate publication.
- Never use replay to evade a blocker, duplicate result, rule review, or reconciliation requirement. A replay starts from step one with fresh external actions and fresh consent gates.
- Treat `blocked` and `failed` as non-success states. Preserve their evidence and recovery instructions.
- Stop when the target duplicate gate reports an existing or disallowed resource.
- Do not reveal cookies, passkeys, announce URLs, API keys, signed URLs, raw torrent bytes, or decrypted credentials.
- Cancellation does not delete downloaded data or audit artifacts. Do not claim otherwise.
- Do not run live tracker, downloader, or image-host probes merely to test connectivity without operator authorization.
- Do not query TMDb/PTGen, probe Sonarr/Radarr, or deliver a test Discord message without operator authorization. A configured endpoint is not consent to contact it except when an explicitly configured job or schedule reaches that declared boundary.
- Never interpret a schedule firing, candidate rank, or notification as permission to submit a candidate or upload a torrent.

## Manage Rules and Configuration

Rule changes follow an immutable review sequence: import Markdown as a draft, inspect the parsed policy and original text, have a human approve the exact fingerprint, then activate the approved revision. Missing, incomplete, unapproved, or stale rules must block automation.

Credentials are write-only inputs. Confirm successful storage from redacted API responses; never try to read secrets back. Keep downloader path mappings explicit and validate them before running jobs.

Discord uses an encrypted incoming `webhook_url`, not the legacy bot token. Select channel names explicitly in `DailyCandidateScheduleConfig.notification_channels`. Inspect `status`, `attempts`, `payload_sha256`, and `remote_receipt` in `/api/v2/notifications`; retry state never authorizes candidate submission or upload.

Sonarr and Radarr are independent read-only metadata helpers. Configure each v3 base endpoint and encrypted `api_key`, explicitly probe it before relying on it, then use lookup with Sonarr `tvdb_id` or `path` plus `title`, or Radarr `tmdb_id` or exact `path`. Treat `matched=false` as a normal miss and continue with other metadata sources. Audit records intentionally store query/response hashes and normalized IDs instead of paths or raw responses.

TMDb and PTGen are independent metadata providers. Configure an explicit endpoint and encrypted `api_key` when required; PTGen must never fall back to an implicit public endpoint. New retorrent jobs expose mandatory `metadata_tmdb` and `metadata_ptgen` boundaries; resolve only when the operator requested the workflow and the job or resume input explicitly names that provider. Treat ambiguous/conflicting IDs, failed calls, missing PTGen text, and `matched=false` as recoverable non-success states. Manual recovery must use only the fields named in the blocker. Raw PTGen text belongs in its verified artifact and must not be copied from a step response or summary; MTEAM packaging rechecks the artifact hash and safely textifies markup. Audit evidence contains hashes and normalized IDs, not credentials, raw responses, or raw descriptions.

Before configuring or invoking a downloader, call `GET /api/v2/downloader-adapters` and honor `runtime_supported`, every `operations` flag, and every `constraints` entry. An unavailable adapter may only be preserved disabled. Never enable it, probe it, or infer support from its name. Transmission, rTorrent, and Deluge report `skip_checking=false`; do not request or simulate that behavior. Deluge uses its Web JSON-RPC endpoint and Web password, requires the Web session to be connected to a daemon, and reports `category=false` and `tags=false`; set `apply_labels=false` explicitly and leave category/tags empty for both source and target downloader controls, never silently discard them or substitute native daemon RPC credentials. For rTorrent, treat an ineffective named-throttle response as a blocker and never claim the requested limit was enforced.

## Migrate Legacy Configuration

1. Call `GET /api/v2/migrations/legacy/preview`. It reads only the fixed read-only mount and never executes Python.
2. Show the operator the exact `source_fingerprint`, resource list, disabled resources, blockers, and warnings. Credential values are intentionally absent.
3. Do not infer consent from a successful preview. Call `POST /api/v2/migrations/legacy` only after the operator explicitly approves that exact fingerprint; send `confirm_import=true`.
4. If the fingerprint changed, stop and preview again. Never reuse an older approval.
5. Treat migration as configuration writes only. It performs no tracker, downloader, image-host, notification, Sonarr, or Radarr probes and grants no permission for live workflow actions. Sonarr/Radarr API settings may migrate; legacy Discord bot credentials cannot become a webhook and require manual replacement.
6. Read import history for the applied resource IDs and 30-day encrypted archive state. Never request, decrypt, expose, or claim access to archive plaintext.
7. Original legacy files are never deleted by migration. Archive expiry removes only the encrypted snapshot and preserves the redacted audit report.

## Interpret Responses

Use `status`, `ok`, `blockers`, `next_actions`, `job_id`, `summary`, `summary_file`, and `resume_state` as the decision path. Keep operator-facing reports concise: current step, verified evidence, blockers, required decision, and next safe action.
