import type {
  CreateJobInput,
  DailyCandidateSchedule,
  DailyCandidateScheduleListEnvelope,
  DailyCandidateScheduleRunListEnvelope,
  DailyCandidateListEnvelope,
  EventsEnvelope,
  Downloader,
	DownloaderAdapterCapability,
  ImageHost,
	MediaManager,
	NotificationChannel,
  JobEnvelope,
  JobListEnvelope,
  JobStatus,
  JobSummaryEnvelope,
  NotificationListEnvelope,
  JsonValue,
  PathMapping,
  RuleRevision,
  ScreenshotProfile,
  SiteCredential,
  SiteSummary,
	LegacyMigrationEnvelope,
	LegacyMigrationPreview,
	LegacyMigrationRecord,
} from "./types";

interface ProblemBody {
  error?: {code?: string; detail?: string};
  blockers?: Array<{code?: string; message?: string}>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class ApiClient {
  constructor(private readonly token: string) {}

  async listJobs(options: {status?: JobStatus | ""; cursor?: string; limit?: number} = {}): Promise<JobListEnvelope> {
    const query = new URLSearchParams({limit: String(options.limit ?? 25)});
    if (options.status) query.set("status", options.status);
    if (options.cursor) query.set("cursor", options.cursor);
    return this.request(`/api/v2/jobs?${query.toString()}`);
  }

  async listDailyCandidates(options: {source: string; target: string; date?: string; limit?: number}): Promise<DailyCandidateListEnvelope> {
    const query = new URLSearchParams({source: options.source.toUpperCase(), target: options.target.toUpperCase(), limit: String(options.limit ?? 25)});
    if (options.date) query.set("date", options.date);
    return this.request(`/api/v2/candidates/daily?${query.toString()}`);
  }

  async createDailyCandidateJob(input: {source: string; target: string; targetCount: number; scanLimit: number; date?: string}): Promise<JobEnvelope> {
    return this.request("/api/v2/candidates/daily", {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({
        source: input.source.toUpperCase(), target: input.target.toUpperCase(),
        target_count: input.targetCount, scan_limit: input.scanLimit,
        date: input.date || undefined, execution_mode: "auto",
      }),
    });
  }

  async submitDailyCandidate(candidateID: string): Promise<JobEnvelope> {
    return this.request(`/api/v2/candidates/${encodeURIComponent(candidateID)}/retorrent-job`, {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({execution_mode: "step"}),
    });
  }

  async listDailyCandidateSchedules(): Promise<DailyCandidateScheduleListEnvelope> {
    return this.request("/api/v2/schedules/daily-candidates?limit=100");
  }

  async createDailyCandidateSchedule(input: {
    name: string; source: string; target: string; cronExpression: string; timezone: string;
    targetCount?: number; scanLimit?: number; notificationChannels?: string[];
  }): Promise<{ok: true; status: "ready"; schedule_id: string; schedule: DailyCandidateSchedule}> {
    return this.request("/api/v2/schedules/daily-candidates", {
      method: "POST",
      body: JSON.stringify({
        name: input.name, cron_expression: input.cronExpression, timezone: input.timezone, enabled: true,
        config: {source: input.source.toUpperCase(), target: input.target.toUpperCase(), target_count: input.targetCount ?? 10, scan_limit: input.scanLimit ?? 30, page: 1, notification_channels: input.notificationChannels ?? []},
      }),
    });
  }

  async setDailyCandidateScheduleEnabled(scheduleID: string, enabled: boolean): Promise<{ok: true; status: "ready"; schedule_id: string; schedule: DailyCandidateSchedule}> {
    return this.request(`/api/v2/schedules/daily-candidates/${encodeURIComponent(scheduleID)}`, {
      method: "PATCH", body: JSON.stringify({enabled}),
    });
  }

  async listDailyCandidateScheduleRuns(scheduleID: string): Promise<DailyCandidateScheduleRunListEnvelope> {
    return this.request(`/api/v2/schedules/daily-candidates/${encodeURIComponent(scheduleID)}/runs?limit=25`);
  }

  async listNotifications(): Promise<NotificationListEnvelope> {
    return this.request("/api/v2/notifications?limit=25");
  }

  async getSummary(jobID: string): Promise<JobSummaryEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/summary`);
  }

  async getEvents(jobID: string): Promise<EventsEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/events?after=0&limit=500`);
  }

  async createJob(input: CreateJobInput): Promise<JobEnvelope> {
		const downloader: Record<string, JsonValue> = {
			name: input.downloaderName,
			save_path: input.savePath,
			apply_labels: input.applyLabels,
			skip_checking: false,
			paused: false,
		};
		if (input.applyLabels) {
			downloader.category = "retorrent-source";
			downloader.tags = ["upload-assistant", "source"];
		}
    const workflowInput: Record<string, JsonValue> = {
      source_url: input.sourceURL,
      target: input.target.toUpperCase(),
      confirm_upload: false,
			downloader,
			target_downloader: {apply_labels: input.applyLabels},
      screenshots: {profile: input.screenshotProfile},
      image_host: {name: input.imageHost},
    };
    const body: Record<string, JsonValue> = {
      kind: "retorrent",
      execution_mode: input.executionMode,
      input: workflowInput,
    };
    if (input.stopAfterStep) body.stop_after_step = input.stopAfterStep;
    return this.request("/api/v2/jobs", {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify(body),
    });
  }

  async transition(jobID: string, action: "pause" | "cancel"): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/${action}`, {method: "POST"});
  }

  async resume(jobID: string, resumeState: Record<string, JsonValue>): Promise<JobEnvelope> {
    return this.request(`/api/v2/jobs/${encodeURIComponent(jobID)}/resume`, {
      method: "POST",
      body: JSON.stringify({resume_state: resumeState}),
    });
  }

  async downloadArtifact(jobID: string, artifactID: string): Promise<Blob> {
    const response = await fetch(`/api/v2/jobs/${encodeURIComponent(jobID)}/artifacts/${encodeURIComponent(artifactID)}/content`, {
      headers: {Authorization: `Bearer ${this.token}`, Accept: "application/octet-stream"},
      credentials: "same-origin",
    });
    if (!response.ok) await this.throwResponseError(response);
    return response.blob();
  }

  async listDownloaders(): Promise<Downloader[]> {
    const response = await this.request<{downloaders: Downloader[]}>("/api/v2/downloaders");
    return response.downloaders;
  }

  async listDownloaderAdapters(): Promise<DownloaderAdapterCapability[]> {
    const response = await this.request<{adapters: DownloaderAdapterCapability[]}>("/api/v2/downloader-adapters");
    return response.adapters;
  }

  async putDownloader(name: string, input: {
    adapter: string;
    endpoint: string;
    credentials: Record<string, string>;
    pathMappings: PathMapping[];
		enabled: boolean;
  }): Promise<void> {
    await this.request(`/api/v2/downloaders/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
		adapter: input.adapter, enabled: input.enabled,
        config: {endpoint: input.endpoint, timeout_seconds: 30, options: {}},
        credentials: input.credentials, path_mappings: input.pathMappings,
      }),
    });
  }

  async probeDownloader(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/downloaders/${encodeURIComponent(name)}/probe`, {method: "POST"});
  }

  async listImageHosts(): Promise<ImageHost[]> {
    const response = await this.request<{image_hosts: ImageHost[]}>("/api/v2/image-hosts");
    return response.image_hosts;
  }

  async listNotificationChannels(): Promise<NotificationChannel[]> {
    const response = await this.request<{notification_channels: NotificationChannel[]}>("/api/v2/notification-channels");
    return response.notification_channels;
  }

  async putNotificationChannel(name: string, input: {enabled: boolean; webhookURL: string}): Promise<void> {
    await this.request(`/api/v2/notification-channels/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({adapter: "discord_webhook", enabled: input.enabled, config: {timeout_seconds: 15, options: {}}, credentials: input.webhookURL ? {webhook_url: input.webhookURL} : {}}),
    });
  }

  async listMediaManagers(): Promise<MediaManager[]> {
    const response = await this.request<{media_managers: MediaManager[]}>("/api/v2/media-managers");
    return response.media_managers;
  }

  async putMediaManager(name: string, input: {adapter: "sonarr" | "radarr"; enabled: boolean; endpoint: string; apiKey: string}): Promise<void> {
    await this.request(`/api/v2/media-managers/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({adapter: input.adapter, enabled: input.enabled, config: {endpoint: input.endpoint, timeout_seconds: 15, options: {}}, credentials: input.apiKey ? {api_key: input.apiKey} : {}}),
    });
  }

  async probeMediaManager(name: string): Promise<JsonValue> {
    return this.request(`/api/v2/media-managers/${encodeURIComponent(name)}/probe`, {method: "POST"});
  }

  async putImageHost(name: string, input: {adapter: string; endpoint: string; apiKey: string; priority: number}): Promise<void> {
    await this.request(`/api/v2/image-hosts/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
        adapter: input.adapter, enabled: true, priority: input.priority,
        config: {endpoint: input.endpoint, timeout_seconds: 30, options: {}},
        credentials: input.apiKey ? {api_key: input.apiKey} : {},
      }),
    });
  }

  async listScreenshotProfiles(): Promise<ScreenshotProfile[]> {
    const response = await this.request<{screenshot_profiles: ScreenshotProfile[]}>("/api/v2/screenshot-profiles");
    return response.screenshot_profiles;
  }

  async createScreenshotProfile(input: {name: string; count: number; format: string; width: number; quality: number; startPercent: number; endPercent: number}): Promise<void> {
    await this.request("/api/v2/screenshot-profiles", {
      method: "POST",
      body: JSON.stringify({name: input.name, enabled: true, config: {
        count: input.count, format: input.format, width: input.width, quality: input.quality,
        start_percent: input.startPercent, end_percent: input.endPercent, comparison: false,
      }}),
    });
  }

  async listSites(): Promise<SiteSummary[]> {
    const response = await this.request<{sites: SiteSummary[]}>("/api/v2/sites");
    return response.sites;
  }

  async listRuleRevisions(siteCode: string): Promise<RuleRevision[]> {
    const response = await this.request<{revisions: RuleRevision[]}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/rules`);
    return response.revisions;
  }

  async importRuleMarkdown(markdown: string): Promise<RuleRevision> {
    const response = await this.request<{revision: RuleRevision}>("/api/v2/site-rules/import", {
      method: "POST", body: JSON.stringify({markdown}),
    });
    return response.revision;
  }

  async approveRule(revision: RuleRevision, comment: string): Promise<void> {
    await this.request(`/api/v2/site-rules/${encodeURIComponent(revision.id)}/approve`, {
      method: "POST", body: JSON.stringify({fingerprint: revision.fingerprint, comment}),
    });
  }

  async activateRule(revisionID: string): Promise<void> {
    await this.request(`/api/v2/site-rules/${encodeURIComponent(revisionID)}/activate`, {method: "POST"});
  }

  async listSiteCredentials(siteCode: string): Promise<SiteCredential[]> {
    const response = await this.request<{credentials: SiteCredential[]}>(`/api/v2/sites/${encodeURIComponent(siteCode)}/credentials`);
    return response.credentials;
  }

  async putSiteCredential(siteCode: string, name: string, value: string): Promise<void> {
    await this.request(`/api/v2/sites/${encodeURIComponent(siteCode)}/credentials/${encodeURIComponent(name)}`, {
      method: "PUT", body: JSON.stringify({value}),
    });
  }

  async previewLegacyMigration(): Promise<LegacyMigrationPreview> {
    return this.request("/api/v2/migrations/legacy/preview");
  }

  async executeLegacyMigration(sourceFingerprint: string): Promise<LegacyMigrationEnvelope> {
    return this.request("/api/v2/migrations/legacy", {
      method: "POST",
      body: JSON.stringify({source_fingerprint: sourceFingerprint, confirm_import: true}),
    });
  }

  async listLegacyMigrations(): Promise<LegacyMigrationRecord[]> {
    const response = await this.request<{imports: LegacyMigrationRecord[]}>("/api/v2/migrations/legacy?limit=25");
    return response.imports;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    if (!response.ok) await this.throwResponseError(response);
    return (await response.json()) as T;
  }

  private async throwResponseError(response: Response): Promise<never> {
    let problem: ProblemBody = {};
    try {
      problem = (await response.json()) as ProblemBody;
    } catch {
      // Keep a bounded generic error when a proxy returns non-JSON.
    }
    const code = problem.error?.code ?? problem.blockers?.[0]?.code ?? "request_failed";
    const detail = problem.error?.detail ?? problem.blockers?.[0]?.message ?? `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, code, detail);
  }
}
