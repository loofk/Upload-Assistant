export type JsonValue = null | boolean | number | string | JsonValue[] | {[key: string]: JsonValue};

export type JobStatus = "draft" | "queued" | "running" | "paused" | "blocked" | "failed" | "complete" | "cancelled";

export interface Blocker {
  code: string;
  message?: string;
  site_code?: string;
  component?: string;
  details?: JsonValue;
}

export interface NextAction {
  action: string;
  description?: string;
  parameters?: JsonValue;
}

export interface Job {
  id: string;
  replay_of_job_id?: string;
  kind: string;
  status: JobStatus;
  execution_mode: "auto" | "step";
  current_step?: string;
  input: Record<string, JsonValue>;
  blockers: Blocker[];
  next_actions: NextAction[];
  resume_state: Record<string, JsonValue>;
  summary: Record<string, JsonValue>;
  created_at: string;
  updated_at: string;
  started_at?: string;
  finished_at?: string;
	idempotent_replay?: boolean;
}

export interface Step {
  id: string;
  job_id: string;
  key: string;
  position: number;
  status: string;
  required: boolean;
  gate_kind?: string;
  input_snapshot: {redacted: true; sha256: string};
  output_summary: JsonValue;
  blockers: Blocker[];
  next_actions: NextAction[];
  resume_state: Record<string, JsonValue>;
  started_at?: string;
  finished_at?: string;
}

export interface StepAttempt {
  id: string;
  job_id: string;
  step_id: string;
  step_key: string;
  step_position: number;
  number: number;
  status: "running" | "paused" | "blocked" | "failed" | "complete" | "cancelled";
  adapter?: string;
  adapter_version?: string;
  input_snapshot: {redacted: true; sha256: string};
  output_summary: JsonValue;
  error_code?: string;
  error_details: JsonValue;
  started_at: string;
  finished_at?: string;
}

export interface StepAttemptListEnvelope {
  ok: true;
  status: JobStatus;
  job_id: string;
  current_step?: string;
  attempts: StepAttempt[];
  has_more: boolean;
  next_cursor: string;
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface Artifact {
  id: string;
  job_id: string;
  step_id?: string;
  attempt_id?: string;
  kind: string;
  storage_backend: string;
  storage_path: string;
  filename: string;
  mime_type?: string;
  size_bytes: number;
  sha256: string;
  metadata: JsonValue;
  expires_at: string;
  created_at: string;
}

export interface AuditEvent {
  id: string;
  job_id: string;
  step_id?: string;
  attempt_id?: string;
  sequence: number;
  type: string;
  actor_type: string;
  actor_id?: string;
  payload: JsonValue;
  previous_hash?: string;
  hash: string;
  created_at: string;
}

export interface GlobalAuditEvent {
  id: string;
  actor_type: string;
  actor_id?: string;
  action: string;
  resource_type: string;
  resource_id?: string;
  trace_id?: string;
  payload: Record<string, JsonValue>;
  created_at: string;
}

export interface AuditEventListEnvelope {
  ok: true;
  status: "ready";
  audit_events: GlobalAuditEvent[];
  has_more: boolean;
  next_cursor: string;
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface LiveReadinessCheck {
  key: string;
  status: "ready" | "blocked";
  summary: string;
  evidence?: Record<string, JsonValue>;
}

export interface LiveRuleConfirmation {
  site_code: string;
  fingerprint: string;
  obligation_ids: string[];
}

export interface LiveReadinessReport {
  ok: boolean;
  status: "configuration_ready" | "blocked";
  configuration_ready: boolean;
  external_calls_performed: false;
  live_upload_authorized: false;
  source: "U2" | "CHD";
  target: "MTEAM";
  checks: LiveReadinessCheck[];
  required_confirmations: LiveRuleConfirmation[];
  blockers: Blocker[];
  next_actions: NextAction[];
  resume_state: {accept_rules: Record<string, JsonValue>; confirm_upload: false};
  summary: string;
}

export interface JobListEnvelope {
  ok: true;
  status: "ready";
  jobs: Job[];
  has_more: boolean;
  next_cursor: string;
}

export interface JobEnvelope {
  ok: boolean;
  status: JobStatus;
  job_id: string;
  replay_of_job_id?: string;
  current_step?: string;
  blockers: Blocker[];
  next_actions: NextAction[];
  resume_state: Record<string, JsonValue>;
  summary: Record<string, JsonValue>;
  job: Job;
}

export interface JobSummaryEnvelope {
  ok: boolean;
  status: JobStatus;
  job_id: string;
	replay_of_job_id?: string;
	kind: string;
  current_step?: string;
  blockers: Blocker[];
  next_actions: NextAction[];
  resume_state: Record<string, JsonValue>;
  summary: Record<string, JsonValue>;
  steps: Step[];
  artifacts: Artifact[];
}

export interface EventsEnvelope {
  ok: true;
  status: "ready";
  job_id: string;
  events: AuditEvent[];
  next_cursor: number;
}

export interface CreateJobInput {
  sourceURL: string;
  target: string;
  executionMode: "auto" | "step";
  stopAfterStep?: string;
  downloaderName: string;
  savePath: string;
	applyLabels: boolean;
  screenshotProfile: string;
  imageHost: string;
  tmdbProvider?: string;
  ptgenProvider?: string;
}

export interface EndpointConfig {
  endpoint: string;
  timeout_seconds?: number;
  options?: Record<string, JsonValue>;
}

export interface PathMapping {
  remote_path: string;
  local_path: string;
  priority?: number;
}

export interface DownloaderAdapterCapability {
  adapter: string;
  display_name: string;
  runtime_supported: boolean;
  credential_fields: string[];
  operations: {
    probe: boolean;
    add_torrent: boolean;
    inspect: boolean;
    list_files: boolean;
    set_limits: boolean;
    wait_complete: boolean;
    category: boolean;
    tags: boolean;
		skip_checking: boolean;
  };
  constraints?: string[];
  unavailable_reason?: string;
}

export interface Downloader {
  id: string;
  name: string;
  adapter: string;
  enabled: boolean;
  config: EndpointConfig;
  credential_fields: string[];
  path_mappings: PathMapping[];
  health_status: string;
  last_health_check_at?: string;
  created_at: string;
  updated_at: string;
  adapter_capability: DownloaderAdapterCapability;
}

export interface ImageHost {
  id: string;
  name: string;
  adapter: string;
  enabled: boolean;
  priority: number;
  config: EndpointConfig;
  credential_fields: string[];
  health_status: string;
  last_health_check_at?: string;
  created_at: string;
  updated_at: string;
}

export interface NotificationChannel {
  id: string;
  name: string;
  adapter: "discord_webhook";
  enabled: boolean;
  config: {timeout_seconds?: number; options?: Record<string, JsonValue>};
  credential_fields: string[];
  health_status: string;
  last_health_check_at?: string;
  created_at: string;
  updated_at: string;
}

export interface MediaManager {
  id: string;
  name: string;
  adapter: "sonarr" | "radarr";
  enabled: boolean;
  config: EndpointConfig;
  credential_fields: string[];
  health_status: string;
  last_health_check_at?: string;
  created_at: string;
  updated_at: string;
}

export interface MetadataProvider {
  id: string;
  name: string;
  adapter: "tmdb" | "ptgen";
  enabled: boolean;
  config: EndpointConfig;
  credential_fields: string[];
  health_status: string;
  last_health_check_at?: string;
  created_at: string;
  updated_at: string;
}

export interface ScreenshotProfile {
  id: string;
  name: string;
  revision: number;
  enabled: boolean;
  config: Record<string, JsonValue>;
  created_at: string;
}

export interface SiteSummary {
  id: string;
  code: string;
  name: string;
  adapter: string;
  enabled: boolean;
  live_validation_status: string;
  active_rule_revision_id?: string;
  active_rule_fingerprint?: string;
}

export interface RuleRevision {
  id: string;
  site_id: string;
  site_code: string;
  revision: number;
  status: "draft" | "approved" | "active" | string;
  fingerprint: string;
  source_url: string;
  captured_at?: string;
  markdown_path: string;
  markdown_sha256: string;
  policy: Record<string, JsonValue>;
  obligations: JsonValue[];
  created_at: string;
}

export interface SiteCredential {
  id: string;
  site_code: string;
  name: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface DailyCandidatePayload {
  source?: {
    tracker?: string;
    torrent_id?: string;
    details_url?: string;
    title?: string;
    size_bytes?: number;
    published_at?: string;
    promotion_labels?: string[];
    free?: boolean;
    downloadable?: boolean;
  };
  metadata?: {
    name?: string;
    imdb_id?: string;
    tmdb_id?: string;
    tmdb_type?: string;
    douban_id?: string;
    anidb_id?: string;
  };
  duplicate_check?: {duplicate?: boolean; result_count?: number};
  ready?: boolean;
  score?: number;
  recommendation_reasons?: string[];
  risks?: Blocker[];
  blockers?: Blocker[];
  next_actions?: NextAction[];
}

export interface DailyCandidate {
  id: string;
  schedule_id?: string;
  discovery_job_id?: string;
  submitted_job_id?: string;
  source_site: string;
  target_site: string;
  source_torrent_id: string;
  recommendation_date: string;
  rank?: number;
  score: number;
  payload: DailyCandidatePayload;
  status: "candidate" | "blocked" | "submitted" | "expired";
  discovered_at: string;
  expires_at: string;
  updated_at: string;
  submitted_at?: string;
  retorrent_action?: {
    method: "POST";
    path: string;
    requires: string[];
  };
}

export interface DailyCandidateListEnvelope {
  ok: true;
  status: "ready";
  date: string;
  count: number;
  ready_count: number;
  candidates: DailyCandidate[];
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface DailyCandidateScheduleConfig {
  source: string;
  target: string;
  target_count: number;
  scan_limit: number;
  page: number;
  notification_channels?: string[];
}

export interface DailyCandidateSchedule {
  id: string;
  name: string;
  kind: "daily_candidates";
  cron_expression: string;
  timezone: string;
  enabled: boolean;
  config: DailyCandidateScheduleConfig;
  next_run_at?: string;
  last_run_at?: string;
  created_at: string;
  updated_at: string;
}

export interface DailyCandidateScheduleListEnvelope {
  ok: true;
  status: "ready";
  count: number;
  schedules: DailyCandidateSchedule[];
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface DailyCandidateScheduleRun {
  id: string;
  schedule_id: string;
  schedule_name: string;
  scheduled_for: string;
  status: "queued" | "running" | "created" | "failed" | "cancelled";
  job_id?: string;
  attempts: number;
  next_attempt_at: string;
  lease_expires_at?: string;
  last_error?: string;
  cron_expression: string;
  timezone: string;
  config: DailyCandidateScheduleConfig;
  created_at: string;
  updated_at: string;
}

export interface DailyCandidateScheduleRunListEnvelope {
  ok: true;
  status: "ready";
  schedule_id: string;
  count: number;
  runs: DailyCandidateScheduleRun[];
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface Notification {
  id: string;
  schedule_run_id?: string;
  job_id?: string;
  notification_channel_id?: string;
  channel: string;
  status: string;
  payload: Record<string, JsonValue>;
  payload_sha256?: string;
  remote_receipt?: Record<string, JsonValue>;
  attempts: number;
  scheduled_at: string;
  sent_at?: string;
  created_at: string;
  updated_at: string;
}

export interface NotificationListEnvelope {
  ok: true;
  status: "ready";
  count: number;
  notifications: Notification[];
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface LegacyMigrationIssue {
  code: string;
  message: string;
  resource?: string;
}

export interface LegacyMigrationPreview {
  ok: boolean;
  status: "ready" | "blocked";
  source_kind: string;
  source_fingerprint: string;
	idempotent_replay?: boolean;
  source_files: Array<{path: string; fingerprint: string; size_bytes: number}>;
  resources: Array<{
    kind: string;
    name: string;
    adapter?: string;
    enabled: boolean;
    credential_fields?: string[];
    configuration?: Record<string, JsonValue>;
  }>;
  archive: {
    encrypted: true;
    retention_days: 30;
    file_count: number;
    uncompressed_bytes: number;
    deletes_originals: false;
    plaintext_available_via_api: false;
  };
  blockers: LegacyMigrationIssue[];
  warnings: LegacyMigrationIssue[];
  next_actions: LegacyMigrationIssue[];
}

export interface LegacyMigrationRecord {
  id: string;
  ok: boolean;
  status: "running" | "blocked" | "failed" | "complete";
  source_kind: string;
  source_path: string;
  source_fingerprint: string;
  report: {
    preview: LegacyMigrationPreview;
    applied: Array<{kind: string; name: string; resource_id?: string; status: string}>;
    blockers: LegacyMigrationIssue[];
    next_actions: LegacyMigrationIssue[];
    summary: string;
  };
  archive_available: boolean;
  archive_sha256?: string;
  archive_size_bytes?: number;
  archive_expires_at: string;
  archive_deleted_at?: string;
  created_at: string;
  updated_at: string;
  finished_at?: string;
}

export interface LegacyMigrationEnvelope {
  ok: boolean;
  status: LegacyMigrationRecord["status"];
  import_id: string;
  source_fingerprint: string;
  blockers: LegacyMigrationIssue[];
  next_actions: LegacyMigrationIssue[];
  summary: string;
  import: LegacyMigrationRecord;
}
