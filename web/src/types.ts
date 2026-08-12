export type JsonValue = null | boolean | number | string | JsonValue[] | {[key: string]: JsonValue};

export interface OperationalLog {id:number;occurred_at:string;level:"debug"|"info"|"warn"|"error";component:string;message:string;request_id?:string;trace_id?:string;job_id?:string;step_key?:string;attempt_id?:string;method?:string;route?:string;status_code?:number;duration_ms?:number;response_bytes?:number;error_code?:string;action?:string;error_detail?:string;attributes?:JsonValue}
export interface OperationalLogListEnvelope {ok:true;status:"ready";operational_logs:OperationalLog[];has_more:boolean;next_cursor:string;blockers:JsonValue[];next_actions:JsonValue[]}
export interface LogAuditContext {id:string;actor_type:string;actor_id?:string;action:string;resource_type:string;resource_id?:string;trace_id?:string;payload:JsonValue;created_at:string}
export interface LogContext {log:OperationalLog;correlated_logs:OperationalLog[];audit_events:LogAuditContext[]}
export interface Incident {id:string;status:"open"|"acknowledged"|"resolved";severity:"info"|"warning"|"critical";kind:string;fingerprint:string;title:string;summary:string;occurrence_count:number;first_occurred_at:string;last_occurred_at:string;job_id?:string;trace_id?:string;evidence:JsonValue}
export interface DiagnosticResult {summary:string;severity:"info"|"warning"|"critical";confidence:number;possible_causes:string[];evidence_refs:string[];recommendations:string[];risks:string[];limitations:string[]}
export interface Diagnostic {id:string;provider_id:string;incident_id?:string;job_id?:string;log_id?:number;status:"queued"|"running"|"failed"|"complete"|"cancelled";data_level:"local"|"remote";provider_config_sha256?:string;evidence_sha256:string;result?:DiagnosticResult;error_code?:string;error_message?:string;created_at:string;finished_at?:string}
export interface FilesystemUsage {name:string;path:string;total_bytes:number;used_bytes:number;available_bytes:number;used_percent:number;status:"ready"|"warning"|"critical"|"unknown"}
export interface OperationsOverview {filesystems:FilesystemUsage[];database_bytes:number;table_bytes:Record<string,number>;operational_log_bytes_30d:number;queued_jobs:number;oldest_queued_job_seconds:number;queued_notifications:number;oldest_notification_seconds:number;open_incidents:number;recent_failures:Incident[];latest_backup?:BackupRun;application_version:string;log_sink_dropped:number;generated_at:string}
export interface OperationsSettings {log_retention_days:number;diagnostic_retention_days:number;filesystem_warning_percent:number;filesystem_critical_percent:number;recovery_hysteresis_percent:number;database_budget_bytes:number;queue_warning_count:number;queue_warning_age_seconds:number;notification_cooldown_seconds:number;auto_diagnostic_incident_kinds:string[];auto_diagnostic_provider_id?:string;updated_at?:string}
export interface BackupRun {id:string;status:string;bundle_sha256?:string;size_bytes?:number;app_version:string;error_code?:string;error_message?:string;created_at:string;verified_at?:string}
export interface APITokenRecord {id:string;prefix:string;name:string;scopes:string[];created_at:string;expires_at?:string;last_used_at?:string;revoked_at?:string;token?:string}
export type ProviderUseCase = "incident_diagnosis" | "rule_analysis";
export type ProviderReasoningEffort = "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export interface ProviderModelCapability {id:string;reasoning_efforts:ProviderReasoningEffort[];reasoning_source:"provider_reported"|"unreported"}
export interface ProviderCapabilities {catalog_source:string;models:ProviderModelCapability[];updated_at?:string}
export interface ProviderProbeEvidence {stage?:"catalog"|"inference";endpoint_path?:string;status_code?:number;content_type?:string;response_sha256?:string;response_shape?:string[];request_id?:string;trace_id?:string;latency_ms?:number;streaming?:boolean;response_headers_ms?:number;stream_event_count?:number;stream_completed?:boolean;error_code?:string;performed_at?:string}
export interface LLMProvider {
  id:string;name:string;kind:"openai_compatible";base_url:string;model:string;data_level:"local"|"remote";
  api_mode:"chat_completions"|"responses";reasoning_effort:ProviderReasoningEffort;use_cases:ProviderUseCase[];json_mode:boolean;streaming_enabled:boolean;
  timeout_seconds:number;enabled:boolean;outbound_consent:boolean;api_key_configured:boolean;
  health_status:"unknown"|"catalog_ready"|"ready"|"failed";last_probe_at?:string;last_probe_latency_ms?:number;last_probe_error_code?:string;
  capabilities:ProviderCapabilities;last_probe_evidence:ProviderProbeEvidence;
}
export interface RuleAnalysisResult {
  draft_markdown:string;source_sha256:string;provider_id:string;provider_name:string;model:string;
  reasoning_effort:ProviderReasoningEffort;source_revision_id?:string;source_complete:boolean;confidence:number;warnings:string[];
  prompt_version:string;external_call_performed:true;
}
export interface BackupPolicy {enabled:boolean;recipient?:string;schedule:string;retention_count:number;updated_at?:string}

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
  attention?: JobAttention;
}

export interface JobAttentionAction {
  id: string;
  label: string;
  description: string;
  kind: "retry" | "safe_repair" | "manual_input" | "manual_review" | "navigate" | string;
  executable: boolean;
  requires_confirmation: boolean;
  href?: string;
}

export interface JobAttention {
  status: string;
  needs_action: boolean;
  issue?: {
    code: string;
    title: string;
    summary: string;
    current_step?: string;
    site_code?: string;
    severity: string;
  };
  solutions: JobAttentionAction[];
}

export interface UploadPreviewEnvelope {
  ok: true;
  status: JobStatus;
  job_id: string;
  package_revision: number;
  package_artifact: Artifact;
  can_revise: boolean;
  package: {
    schema_version: number;
    target: string;
    adapter: string;
    source: Record<string, JsonValue>;
    metadata_links: Record<string, string>;
    form_fields: Record<string, JsonValue>;
    description: string;
    mediainfo: JsonValue;
    content: {file_count: number; total_size_bytes: number; manifest_sha256: string};
    evidence: Record<string, JsonValue>;
    decisions: Array<{field: string; value: JsonValue; derivation: string; evidence?: string}>;
    warnings: string[];
    naming_profiles?: Array<{id: string; label: string; release_title: {required: boolean; pattern: string; template?: string; max_length?: number; evidence_refs?: string[]}}>;
    manual_review_required: boolean;
    generated_at: string;
  };
  blockers: Blocker[];
  next_actions: NextAction[];
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
		list_torrents: boolean;
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

export type DownloaderTorrentGroup = "downloading" | "seeding" | "paused" | "checking" | "error" | "completed";

export interface DownloaderDashboardSummary {
	total: number;
	downloading: number;
	seeding: number;
	paused: number;
	checking: number;
	errors: number;
	active: number;
	download_speed: number;
	upload_speed: number;
}

export interface DownloaderDashboardTorrent {
	hash: string;
	name: string;
	state: string;
	state_group: DownloaderTorrentGroup;
	progress: number;
	total_size: number;
	amount_left: number;
	downloaded: number;
	uploaded: number;
	download_speed: number;
	upload_speed: number;
	download_limit: number;
	upload_limit: number;
	limits_available: boolean;
	ratio: number;
	category?: string;
	tags?: string;
	added_on: number;
	completion_on: number;
	time_active: number;
	seeding_time: number;
}

export interface DownloaderDashboardSnapshot {
	downloader_name: string;
	adapter: string;
	network_class: "unknown" | "home" | "seedbox";
	fetched_at: string;
	summary: DownloaderDashboardSummary;
	torrents: DownloaderDashboardTorrent[];
	filtered_total: number;
	offset: number;
	limit: number;
	has_more: boolean;
}

export interface DownloaderTorrentFile {
	index: number;
	name: string;
	size: number;
	progress: number;
	priority: number;
	is_seed: boolean;
	availability: number;
}

export interface DownloaderTorrentFilesEvidence {
	files: DownloaderTorrentFile[];
	file_count: number;
	total_size: number;
}

export interface AdapterCapability {
  id: string;
  kind: "downloader" | "image_host" | "media_analyzer" | "media_manager" | "metadata_provider" | "notification_channel" | "screenshot_engine" | "site" | "torrent_maker";
  adapter: string;
  display_name: string;
  site_code?: string;
  runtime_supported: boolean;
  operations: string[];
  credential_fields: string[];
  safety_gates: string[];
  constraints: string[];
  unavailable_reason?: string;
}

export interface AdapterCatalogEnvelope {
  ok: true;
  status: "ready";
  catalog_version: string;
  catalog_sha256: string;
  count: number;
  adapters: AdapterCapability[];
  blockers: Blocker[];
  next_actions: NextAction[];
}

export interface Downloader {
  id: string;
  name: string;
  adapter: string;
  enabled: boolean;
  network_class: "unknown" | "home" | "seedbox";
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
  adapter: "discord_webhook" | "telegram_bot" | "wecom_bot" | "feishu_bot";
  enabled: boolean;
  config: {timeout_seconds?: number; event_types?: string[]; options?: Record<string, JsonValue>};
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
  rule_revision_count: number;
  aliases: string[];
  tags: string[];
}

export interface RuleRevision {
  id: string;
  site_id: string;
  site_code: string;
  revision: number;
  status: "draft" | "approved" | "retired" | string;
  fingerprint: string;
  source_url: string;
  captured_at?: string;
  markdown_path: string;
  markdown_sha256: string;
  policy: Record<string, JsonValue>;
  obligations: JsonValue[];
  created_at: string;
}

export interface RuleReviewCheck {
  section: string;
  decision: "confirmed" | "needs_changes";
  comment?: string;
  fingerprint: string;
  reviewer_id: string;
  updated_at: string;
}

export interface RuleReviewSection {
  key: string;
  title: string;
  status: "extracted" | "partially_extracted" | "not_extracted" | "evidence" | string;
  summary: string;
  facts: Array<{label: string; value: string; detail?: string; tone?: "positive" | "warning" | "danger" | "neutral" | string}>;
  data: Record<string, JsonValue>;
  check?: RuleReviewCheck;
}

export interface RuleReviewWorkspace {
  revision_id: string;
  site_code: string;
  fingerprint: string;
  revision_status: string;
  approval_ready: boolean;
  confirmed_count: number;
  required_count: number;
  sections: RuleReviewSection[];
  advisories: Array<{section: string; severity: "info" | "warning" | string; summary: string; evidence_refs?: string[]}>;
  blockers: Array<{code: string; section?: string; message: string; evidence_refs?: string[]}>;
  next_actions: Array<Record<string, JsonValue>>;
}

export interface SiteCredential {
  id: string;
  site_code: string;
  name: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface SiteAccessPolicyInput {
  enabled: boolean;
  general_min_interval_seconds: number;
  general_max_requests_per_hour: number;
  search_min_interval_seconds: number;
  search_max_requests_per_hour: number;
  max_concurrency: number;
}

export interface SiteAccessPolicy extends SiteAccessPolicyInput {
  site_code: string;
  service_access: "allowed" | "forbidden" | "undetermined" | "";
  search_access: "allowed" | "forbidden" | "undetermined" | "";
  rule_revision_id?: string;
  rule_fingerprint?: string;
  rule_schema_version: number;
  policy_fingerprint?: string;
  operator_policy?: SiteAccessPolicyInput;
  active_requests: number;
  general_used_this_hour: number;
  search_used_this_hour: number;
  general_cooldown_until?: string;
  search_cooldown_until?: string;
  blockers: Array<{code: string; message: string}>;
}

export interface RuleSourceInput {
  id: string;
  url: string;
  scope: string;
  auth_mode?: "none" | "site_cookie";
}

export interface RuleSourceSet {
  site_code: string;
  sources: RuleSourceInput[];
  fingerprint: string;
  scope_confirmed: boolean;
  cookie_hosts_confirmed: boolean;
  cookie_configured: boolean;
  cookie_required: boolean;
  updated_at?: string;
}

export interface RuleCollectionDocument {
  id: string;
  source_id: string;
  url: string;
  scope: string;
  auth_mode: "none" | "site_cookie";
  status: "pending" | "fetching" | "ready" | "failed" | string;
  http_status?: number;
  content_type?: string;
  size_bytes?: number;
  text_sha256?: string;
  error_code?: string;
  error_detail?: string;
  captured_at?: string;
}

export interface RuleCollectionRun {
  id: string;
  site_code: string;
  source_set_fingerprint: string;
  provider_id: string;
  provider_config_sha256?: string;
  status: "queued" | "fetching" | "analyzing" | "ready" | "failed" | string;
  not_before: string;
  rule_revision_id?: string;
  error_code?: string;
  error_detail?: string;
  documents: RuleCollectionDocument[];
  created_at: string;
  started_at?: string;
  completed_at?: string;
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
